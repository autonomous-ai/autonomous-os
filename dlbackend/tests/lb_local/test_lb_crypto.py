"""Tests for LB encryption pipeline — HTTP and WebSocket.

Uses monkeypatching to mock backends. Crypto is initialized in-memory (no disk).
"""

import base64
import json
import os
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from core.crypto.rsa_aes import AESGCMSession, RSAAESCrypto
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import (
    CipherHTTPRequest,
    CipherHTTPResponse,
    WSCipherMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(crypto: RSAAESCrypto) -> tuple[AESGCMSession, bytes]:
    """Generate a session key, RSA-encrypt it, return (session, encrypted_key_bytes)."""
    session_key = os.urandom(32)
    encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)
    return AESGCMSession(session_key), encrypted_key


def _encrypt_body(session: AESGCMSession, encrypted_key: bytes, plain: bytes) -> bytes:
    """Build an CipherHTTPRequest JSON from plain bytes."""
    encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain))
    req = CipherHTTPRequest(
        encrypted_key=base64.b64encode(encrypted_key).decode(),
        nonce=base64.b64encode(encrypted.nonce).decode(),
        cipher_data=base64.b64encode(encrypted.cipher_data).decode(),
    )
    return req.model_dump_json().encode()


def _decrypt_response(session: AESGCMSession, content: bytes) -> bytes:
    """Decrypt an CipherHTTPResponse JSON back to plain bytes."""
    enc_resp = CipherHTTPResponse.model_validate_json(content)
    payload = enc_resp.to_raw_payload()
    return session.decrypt(payload).plain_data


def _mock_response(request: httpx.Request) -> httpx.Response:
    """Mock backend that echoes the request body."""
    return httpx.Response(
        200,
        json={
            "path": str(request.url.path),
            "body": request.content.decode() if request.content else "",
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def crypto() -> RSAAESCrypto:
    return RSAAESCrypto()


@pytest.fixture()
def lb_client(monkeypatch, crypto):
    """LB TestClient with crypto enabled and mocked HTTP backend."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    monkeypatch.setattr("lbserver.app.BACKENDS", ["http://backend:8001"])
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(["http://backend:8001"]))
    monkeypatch.setattr(
        "lbserver.app._client",
        httpx.AsyncClient(transport=httpx.MockTransport(_mock_response)),
    )
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


# ---------------------------------------------------------------------------
# Public key endpoint
# ---------------------------------------------------------------------------


class TestPublicKeyEndpoint:
    def test_returns_valid_pem(self, lb_client, crypto):
        resp = lb_client.get("/api/dl/public-key")
        assert resp.status_code == 200
        pem = resp.text
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")
        assert pem == crypto.public_key_pem

    def test_returns_404_when_disabled(self, monkeypatch):
        from lbserver.utils.state import set_crypto

        set_crypto(None)
        from lbserver.app import app
        client = TestClient(app)
        resp = client.get("/api/dl/public-key")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# HTTP encryption pipeline
# ---------------------------------------------------------------------------


class TestHTTPEncryption:
    def test_encrypted_request_decrypted_before_forwarding(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        plain_body = json.dumps({"image_b64": "abc123"}).encode()
        encrypted_body = _encrypt_body(session, encrypted_key, plain_body)

        resp = lb_client.post("/api/dl/pose-estimate", content=encrypted_body)
        assert resp.status_code == 200

        # Response is encrypted
        decrypted = _decrypt_response(session, resp.content)
        data = json.loads(decrypted)
        # Backend received the plain body
        assert json.loads(data["body"]) == {"image_b64": "abc123"}

    def test_full_round_trip(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        plain = json.dumps({"test": "hello"}).encode()
        encrypted_body = _encrypt_body(session, encrypted_key, plain)

        resp = lb_client.post("/api/dl/test", content=encrypted_body)
        assert resp.status_code == 200

        decrypted = _decrypt_response(session, resp.content)
        data = json.loads(decrypted)
        assert data["path"] == "/api/dl/test"
        assert json.loads(data["body"]) == {"test": "hello"}

    def test_plain_request_passes_through(self, lb_client):
        resp = lb_client.post("/api/dl/test", json={"plain": True})
        assert resp.status_code == 200
        # Response is plain (not encrypted)
        data = resp.json()
        assert "path" in data

    def test_tampered_ciphertext_returns_400(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=b"data"))

        req = CipherHTTPRequest(
            encrypted_key=base64.b64encode(encrypted_key).decode(),
            nonce=base64.b64encode(encrypted.nonce).decode(),
            cipher_data=base64.b64encode(encrypted.cipher_data + b"\xff").decode(),
        )
        resp = lb_client.post("/api/dl/test", content=req.model_dump_json().encode())
        assert resp.status_code == 400
        assert "invalid auth tag" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# WebSocket encryption pipeline
# ---------------------------------------------------------------------------

WS_ECHO_PORT = 19050


def _make_ws_echo_app() -> FastAPI:
    """A tiny WS echo server that sends back whatever it receives."""
    ws_app = FastAPI()

    @ws_app.websocket("/{path:path}")
    async def echo(ws: WebSocket, path: str):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_text()
                await ws.send_text(data)
        except WebSocketDisconnect:
            pass

    return ws_app


@pytest.fixture(scope="module", autouse=True)
def ws_echo_server():
    """Start a WS echo backend for the test module."""
    app = _make_ws_echo_app()
    t = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": WS_ECHO_PORT, "log_level": "error"},
        daemon=True,
    )
    t.start()
    time.sleep(0.5)
    yield


@pytest.fixture()
def lb_ws_client(monkeypatch, crypto):
    """LB TestClient with crypto enabled, WS backend pointing to echo server."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    backends = [f"http://127.0.0.1:{WS_ECHO_PORT}"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    monkeypatch.setattr("lbserver.app.ws_rr", RoundRobin(backends))
    monkeypatch.setattr(
        "lbserver.app._client",
        httpx.AsyncClient(transport=httpx.MockTransport(_mock_response)),
    )
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


class TestWSEncryption:
    def test_key_exchange(self, lb_ws_client, crypto):
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({
                "type": "key_exchange",
                "encrypted_key": base64.b64encode(encrypted_key).decode(),
            })
            resp = ws.receive_json()
            assert resp["status"] == "key_exchange_ok"

    def test_encrypted_message_round_trip(self, lb_ws_client, crypto):
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)
        session = AESGCMSession(session_key)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            # Key exchange
            ws.send_json({
                "type": "key_exchange",
                "encrypted_key": base64.b64encode(encrypted_key).decode(),
            })
            resp = ws.receive_json()
            assert resp["status"] == "key_exchange_ok"

            # Send encrypted message
            plain = json.dumps({"type": "frame", "task": "pose", "frame_b64": "abc"})
            encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain.encode()))
            ws_msg = WSCipherMessage.from_raw_payload(encrypted)
            ws.send_text(ws_msg.model_dump_json())

            # Receive encrypted response (echo)
            raw_resp = ws.receive_text()
            enc_resp = WSCipherMessage.model_validate_json(raw_resp)
            decrypted = session.decrypt(enc_resp.to_raw_payload())
            assert json.loads(decrypted.plain_data) == json.loads(plain)

    def test_plain_ws_without_key_exchange(self, lb_ws_client):
        """Without key exchange, messages pass through unencrypted."""
        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            # Send plain (not key_exchange, just a normal message)
            ws.send_json({"type": "heartbeat", "task": "pose"})
            resp = ws.receive_json()
            assert resp == {"type": "heartbeat", "task": "pose"}
