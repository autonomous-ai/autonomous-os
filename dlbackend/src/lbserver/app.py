"""DL Backend Load Balancer — round-robin reverse proxy with encryption.

All incoming requests are prefixed with /_internal and forwarded back
through nginx, which routes /_internal/hal/ → :8001 (DL server)
and /_internal/ → :8000 (old DL server), stripping the prefix.

When crypto is enabled, the LB handles encryption/decryption:
- GET /api/crypto/public-key returns the RSA public key
- HTTP: CipherHTTPRequest decrypted before forwarding, response encrypted
- WS: WSKeyExchangeRequest first, then WSCipherMessage both directions
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from config import settings
from core.crypto.rsa_aes import AESGCMSession, RSAAESCrypto
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import WSCipherMessage, WSKeyExchangeRequest
from lbserver.routes.crypto import router as crypto_router
from lbserver.utils import RoundRobin
from lbserver.utils.crypto import encrypt_http_response, try_decrypt_http_body
from lbserver.utils.state import get_crypto, set_crypto

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
logger = logging.getLogger("lbserver")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INTERNAL_PREFIX: str = settings.lb.internal_prefix
BACKENDS: list[str] = [b.strip().rstrip("/") for b in settings.lb.backends.split(",") if b.strip()]

if not BACKENDS:
    logger.warning("No backends configured — set LB__BACKENDS=http://127.0.0.1:8888")


http_rr = RoundRobin(BACKENDS)
ws_rr = RoundRobin(BACKENDS)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Initialize crypto if enabled
    if settings.crypto.enabled:
        crypto = RSAAESCrypto(
            key_dir=settings.crypto.key_dir,
            key_size=settings.crypto.key_size,
        )
        set_crypto(crypto)
        logger.info("Encryption enabled (key_dir=%s)", settings.crypto.key_dir)
    yield
    set_crypto(None)


app = FastAPI(title="DL Backend Load Balancer", lifespan=_lifespan)
app.include_router(crypto_router, prefix="/api/crypto")


# ---------------------------------------------------------------------------
# HTTP reverse proxy (all methods, all paths)
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_http(request: Request, path: str) -> Response:
    backend: str = http_rr.next()
    url: str = f"{backend}{INTERNAL_PREFIX}/{path}"

    headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding", "content-length")
    }

    body: bytes = await request.body()
    encrypted_key: bytes | None = None

    # HTTP is stateless (no persistent session like WS), so each encrypted request
    # carries its own RSA-wrapped AES key. We capture that `encrypted_key` here and
    # reuse it below to encrypt the RESPONSE with the same AES session — that is the
    # only thing tying request and response together. `encrypted_key is not None`
    # therefore doubles as the "this exchange is encrypted" flag.
    # When crypto is off (or the body is plaintext and require_encryption is false)
    # `try_decrypt_http_body` returns the body unchanged and encrypted_key stays None.
    if request.method in ("POST", "PUT", "PATCH") and body:
        body, encrypted_key = try_decrypt_http_body(body)

    try:
        async with httpx.AsyncClient(timeout=settings.lb.http_timeout) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                params=dict(request.query_params),
                content=body,
            )
    except httpx.ConnectError:
        logger.error("[HTTP] Backend unreachable: %s", backend)
        raise HTTPException(status_code=502, detail=f"Backend unreachable: {backend}")

    content = resp.content
    resp_headers = dict(resp.headers)

    logger.info(
        "[HTTP] %s /%s → %s (encrypted=%s): %s %s",
        request.method,
        path,
        url,
        encrypted_key is not None,
        resp.status_code,
        resp.text[:100],
    )

    # Encrypt response if request was encrypted
    if encrypted_key is not None:
        content = encrypt_http_response(content, encrypted_key)
        resp_headers["content-type"] = "application/json"
        resp_headers.pop("content-length", None)

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# WebSocket reverse proxy
# ---------------------------------------------------------------------------


@app.websocket("/{path:path}")
async def proxy_ws(client_ws: WebSocket, path: str) -> None:
    backend: str = ws_rr.next()
    ws_backend: str = backend.replace("http://", "ws://").replace("https://", "wss://")
    ws_url: str = f"{ws_backend}{INTERNAL_PREFIX}/{path}"

    # Forward auth headers
    extra_headers: dict[str, str] = {}
    for key in ("x-api-key", "authorization"):
        val: str | None = client_ws.headers.get(key)
        if val:
            extra_headers[key] = val

    await client_ws.accept()
    logger.info("[WS] /%s → %s", path, ws_url)

    crypto = get_crypto()
    session: AESGCMSession | None = None

    # Handle key exchange BEFORE connecting to the backend. Unlike HTTP, a WS
    # connection is long-lived, so we establish one AES session up front and reuse it
    # for every frame in both directions.
    #
    # Close-code convention (RFC 6455):
    #   1008 (policy violation) — client failed to follow the required handshake
    #       (no key-exchange frame, or it didn't validate) while encryption is
    #       mandatory. It's the client's fault, so we signal a policy breach.
    #   1011 (internal error)   — the key-exchange frame WAS well-formed but
    #       decryption/session setup threw (bad RSA key, tampered payload). The
    #       failure is server-side crypto, so we signal an internal error.
    #
    # require_encryption gating:
    #   - true  → a missing/invalid handshake closes the socket (fail closed).
    #   - false → we fall through with session=None and `first_msg` preserved, so
    #       the connection proceeds as PLAINTEXT (the first message is forwarded
    #       verbatim to the backend below). This is the dev/back-compat path.
    first_msg: str | None = None
    if crypto is not None:
        try:
            first_msg = await asyncio.wait_for(client_ws.receive_text(), timeout=5.0)
            try:
                key_req = WSKeyExchangeRequest.model_validate_json(first_msg)
                session = crypto.create_session(key_req.to_raw_key())
                await client_ws.send_json({"status": "key_exchange_ok"})
                logger.info("[WS] /%s → %s: Encrypted session established", path, ws_url)
                first_msg = None  # consumed — it was the handshake, not real traffic
            except ValidationError:
                # First frame wasn't a key exchange. Reject only if encryption is required;
                # otherwise keep first_msg and treat the connection as plaintext.
                if settings.crypto.require_encryption:
                    await client_ws.close(code=1008, reason="Key exchange required")
                    return
        except asyncio.TimeoutError:
            # Client sent nothing within 5s — same policy as a missing handshake.
            if settings.crypto.require_encryption:
                await client_ws.close(code=1008, reason="Key exchange required")
                return
            first_msg = None
        except (ValueError, InvalidTag) as e:
            # Handshake parsed but the wrapped AES key could not be decrypted.
            logger.error("[WS] /%s → %s: Key exchange failed: %s", path, ws_url, e)
            await client_ws.close(code=1011, reason=f"Key exchange failed: {e}")
            return

    try:
        async with websockets.connect(ws_url, additional_headers=extra_headers, open_timeout=settings.lb.ws_open_timeout) as backend_ws:
            # Forward the first message if it wasn't a key exchange
            if first_msg is not None:
                await backend_ws.send(first_msg)

            async def client_to_backend() -> None:
                try:
                    while True:
                        data: str = await client_ws.receive_text()

                        if session is not None:
                            try:
                                enc_msg = WSCipherMessage.model_validate_json(data)
                                result = session.decrypt(enc_msg.to_raw_payload())
                                data = result.plain_data.decode()
                            except ValidationError:
                                logger.warning(
                                    "[WS] /%s → %s: Rejecting unencrypted message",
                                    path,
                                    ws_url,
                                )
                                continue
                            except (InvalidTag, ValueError) as e:
                                logger.error("[WS] /%s → %s: Decrypt failed: %s", path, ws_url, e)
                                continue
                        logger.info(
                            "[WS] /%s → %s (encrypted=%s): %s",
                            path,
                            ws_url,
                            session is not None,
                            data[:100],
                        )
                        await backend_ws.send(data)
                except WebSocketDisconnect:
                    await backend_ws.close()

            async def backend_to_client() -> None:
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            logger.info(
                                "[WS] %s → /%s (encrypted=%s): %s",
                                ws_url,
                                path,
                                session is not None,
                                msg[:100],
                            )
                            if session is not None:
                                encrypted = session.encrypt(
                                    AESGCMPlainPayload(plain_data=msg.encode())
                                )
                                msg = WSCipherMessage.from_raw_payload(encrypted).model_dump_json()
                            await client_ws.send_text(msg)
                        else:
                            logger.info(
                                "[WS] %s → /%s (encrypted=%s): %s",
                                ws_url,
                                path,
                                session is not None,
                                msg[:100].decode(),
                            )
                            if session is not None:
                                encrypted = session.encrypt(AESGCMPlainPayload(plain_data=msg))
                                msg = WSCipherMessage.from_raw_payload(encrypted).model_dump_json()
                                await client_ws.send_text(msg)
                            else:
                                await client_ws.send_bytes(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            tasks = [
                asyncio.create_task(client_to_backend()),
                asyncio.create_task(backend_to_client()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception():
                        logger.warning("[WS] proxy task failed: %s", t.exception())
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    except (websockets.exceptions.InvalidStatus, OSError) as e:
        logger.error("[WS] Backend connection failed: %s — %s", backend, e)
        await client_ws.close(code=1011, reason=f"Backend unreachable: {backend}")
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected: /%s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DL Backend Load Balancer")
    parser.add_argument("--host", default=settings.lb.host)
    parser.add_argument("--port", type=int, default=settings.lb.port)
    parser.add_argument("--log-dir", default=None, help="Directory for rotating log files")
    parser.add_argument("--pid-file", default=None, help="Write PID to this file")
    return parser.parse_args()


def _setup_logging(log_dir: str | None) -> dict[str, Any] | None:
    """Configure application logging. Returns uvicorn log_config dict (or None for console)."""
    if not log_dir:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        return None

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(log_dir) / "lbserver.log"
        uvicorn_log_path = Path(log_dir) / "uvicorn.log"
        # Rotate old logs
        for prefix in ("lbserver.log", "uvicorn.log"):
            for bak in Path(log_dir).glob(f"{prefix}*.bak"):
                bak.unlink()
            for old in Path(log_dir).glob(f"{prefix}*"):
                old.rename(Path(str(old) + ".bak"))
        handler = logging.handlers.RotatingFileHandler(str(log_path), maxBytes=1_048_576, backupCount=3)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.basicConfig(level=logging.INFO, handlers=[handler])

        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": LOG_FORMAT},
                "access": {"format": LOG_FORMAT},
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": str(uvicorn_log_path),
                    "maxBytes": 1_048_576,
                    "backupCount": 3,
                },
                "access": {
                    "formatter": "access",
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": str(uvicorn_log_path),
                    "maxBytes": 1_048_576,
                    "backupCount": 3,
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"level": "INFO"},
                "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            },
        }
    except Exception as e:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logging.getLogger(__name__).warning("File logging setup failed, using console: %s", e)
        return None


def main() -> None:
    args = parse_args()
    uvicorn_log_config = _setup_logging(args.log_dir)

    def _handle_sigterm(signum, frame):
        logger.critical("SIGTERM received — shutting down (pid=%d)", os.getpid())

    signal.signal(signal.SIGTERM, _handle_sigterm)

    if args.pid_file:
        try:
            Path(args.pid_file).write_text(str(os.getpid()))
        except Exception as e:
            logger.warning("Failed to write PID file %s: %s", args.pid_file, e)

    if BACKENDS:
        logger.info("Backends: %s", ", ".join(BACKENDS))
    else:
        logger.error("No backends — set LB__BACKENDS in .env")
    logger.info("Internal prefix: %s", INTERNAL_PREFIX)
    logger.info("Starting load balancer on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_config=uvicorn_log_config)
