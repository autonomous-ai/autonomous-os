"""DL Backend Load Balancer — round-robin reverse proxy with encryption.

All incoming requests are prefixed with /_internal and forwarded back
through nginx, which routes /_internal/lelamp/ → :8001 (DL server)
and /_internal/ → :8000 (old DL server), stripping the prefix.

When crypto is enabled, the LB handles encryption/decryption:
- GET /api/crypto/public-key returns the RSA public key
- HTTP: CipherHTTPRequest decrypted before forwarding, response encrypted
- WS: WSKeyExchangeRequest first, then WSCipherMessage both directions
"""

import argparse
import asyncio
import logging
import os

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
BACKENDS: list[str] = [
    b.strip().rstrip("/") for b in settings.lb.backends.split(",") if b.strip()
]

if not BACKENDS:
    logger.warning("No backends configured — set LB__BACKENDS=http://127.0.0.1:8888")


http_rr = RoundRobin(BACKENDS)
ws_rr = RoundRobin(BACKENDS)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


from contextlib import asynccontextmanager


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
_client = httpx.AsyncClient(timeout=120.0)


# ---------------------------------------------------------------------------
# HTTP reverse proxy (all methods, all paths)
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_http(request: Request, path: str) -> Response:
    backend: str = http_rr.next()
    url: str = f"{backend}{INTERNAL_PREFIX}/{path}"

    headers: dict[str, str] = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding", "content-length")
    }

    body: bytes = await request.body()
    encrypted_key: bytes | None = None

    # Decrypt if encrypted
    if request.method in ("POST", "PUT", "PATCH") and body:
        body, encrypted_key = try_decrypt_http_body(body)

    logger.info("[HTTP] %s /%s → %s (encrypted=%s)", request.method, path, url, encrypted_key is not None)

    try:
        resp = await _client.request(
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

    # Handle key exchange before connecting to backend
    first_msg: str | None = None
    if crypto is not None:
        try:
            first_msg = await asyncio.wait_for(client_ws.receive_text(), timeout=5.0)
            try:
                key_req = WSKeyExchangeRequest.model_validate_json(first_msg)
                session = crypto.create_session(key_req.to_raw_key())
                await client_ws.send_json({"status": "key_exchange_ok"})
                logger.info("[WS] Session established for /%s", path)
                first_msg = None  # consumed
            except ValidationError:
                if settings.crypto.require_encryption:
                    await client_ws.close(code=1008, reason="Key exchange required")
                    return
        except asyncio.TimeoutError:
            if settings.crypto.require_encryption:
                await client_ws.close(code=1008, reason="Key exchange required")
                return
            first_msg = None
        except (ValueError, InvalidTag) as e:
            logger.error("[WS] Key exchange failed: %s", e)
            await client_ws.close(code=1011, reason=f"Key exchange failed: {e}")
            return

    try:
        async with websockets.connect(ws_url, additional_headers=extra_headers) as backend_ws:

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
                                logger.warning("[WS] Rejecting unencrypted message")
                                continue
                            except (InvalidTag, ValueError) as e:
                                logger.error("[WS] Decrypt failed: %s", e)
                                continue

                        await backend_ws.send(data)
                except WebSocketDisconnect:
                    await backend_ws.close()

            async def backend_to_client() -> None:
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            if session is not None:
                                encrypted = session.encrypt(AESGCMPlainPayload(plain_data=msg.encode()))
                                msg = WSCipherMessage.from_raw_payload(encrypted).model_dump_json()
                            await client_ws.send_text(msg)
                        else:
                            await client_ws.send_bytes(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(client_to_backend(), backend_to_client())

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


def _setup_logging(log_dir: str | None) -> None:
    if log_dir:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        Path(log_dir).mkdir(parents=True, exist_ok=True)
        for bak in Path(log_dir).glob("lbserver.log*.bak"):
            bak.unlink()
        log_path = Path(log_dir) / "lbserver.log"
        if log_path.exists():
            log_path.rename(log_path.with_suffix(".log.bak"))
        for old in Path(log_dir).glob("lbserver.log.*"):
            old.rename(Path(str(old) + ".bak"))
        handler = RotatingFileHandler(
            str(log_path), maxBytes=1_048_576, backupCount=3
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def main() -> None:
    args = parse_args()
    _setup_logging(args.log_dir)

    if args.pid_file:
        from pathlib import Path

        Path(args.pid_file).write_text(str(os.getpid()))

    if BACKENDS:
        logger.info("Backends: %s", ", ".join(BACKENDS))
    else:
        logger.error("No backends — set LB__BACKENDS in .env")
    logger.info("Internal prefix: %s", INTERNAL_PREFIX)
    logger.info("Starting load balancer on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)
