"""Client-side RSA+AES-GCM encryption for DL backend communication.

Mirrors the wire format defined in dlbackend/src/lbserver/models.py.
Public key can be provided via DL_PUBLIC_KEY_PEM env var or fetched from
the load balancer's GET /api/dl/public-key endpoint.
"""

import base64
import logging
import os
from typing import Literal

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel

logger = logging.getLogger(__name__)

GCM_NONCE_SIZE: int = 12


# ---------------------------------------------------------------------------
# Wire-format models (mirrors dlbackend/src/lbserver/models.py)
# ---------------------------------------------------------------------------


class CipherPayload(BaseModel):
    """Base AES-GCM cipher fields (shared by HTTP response + WS message)."""

    nonce: str        # base64
    cipher_data: str  # base64


class CipherHTTPRequest(CipherPayload):
    """HTTP request with encrypted payload + RSA-encrypted AES key."""

    encrypted_key: str  # base64


class CipherHTTPResponse(CipherPayload):
    """HTTP response with encrypted payload (AES-only)."""


class WSKeyExchangeRequest(BaseModel):
    """WS key exchange: client sends RSA-encrypted AES session key."""

    type: Literal["key_exchange"] = "key_exchange"
    encrypted_key: str  # base64


class WSCipherMessage(CipherPayload):
    """WS encrypted message (after key exchange, both directions)."""

    type: Literal["encrypted"] = "encrypted"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class CryptoSession:
    """AES-256-GCM session backed by an RSA-encrypted session key."""

    def __init__(self, public_key: RSAPublicKey) -> None:
        self._session_key: bytes = os.urandom(32)
        self._aesgcm: AESGCM = AESGCM(self._session_key)

        oaep = padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )
        self._encrypted_key: bytes = public_key.encrypt(self._session_key, oaep)
        self.encrypted_key_b64: str = base64.b64encode(self._encrypted_key).decode()

    def encrypt(self, plaintext: bytes) -> CipherPayload:
        nonce = os.urandom(GCM_NONCE_SIZE)
        cipher_data = self._aesgcm.encrypt(nonce, plaintext, None)
        return CipherPayload(
            nonce=base64.b64encode(nonce).decode(),
            cipher_data=base64.b64encode(cipher_data).decode(),
        )

    def decrypt(self, payload: CipherPayload) -> bytes:
        nonce = base64.b64decode(payload.nonce)
        cipher_data = base64.b64decode(payload.cipher_data)
        return self._aesgcm.decrypt(nonce, cipher_data, None)

    # -- HTTP helpers --

    def wrap_http_request(self, plain_body: bytes) -> bytes:
        """Encrypt plain body into a CipherHTTPRequest JSON."""
        enc = self.encrypt(plain_body)
        req = CipherHTTPRequest(
            encrypted_key=self.encrypted_key_b64,
            nonce=enc.nonce,
            cipher_data=enc.cipher_data,
        )
        return req.model_dump_json().encode()

    def unwrap_http_response(self, resp_body: bytes) -> bytes:
        """Decrypt a CipherHTTPResponse JSON back to plain bytes."""
        resp = CipherHTTPResponse.model_validate_json(resp_body)
        return self.decrypt(resp)

    # -- WS helpers --

    def wrap_ws_message(self, plain: str) -> str:
        """Encrypt a plain WS text message into a WSCipherMessage JSON."""
        enc = self.encrypt(plain.encode())
        msg = WSCipherMessage(nonce=enc.nonce, cipher_data=enc.cipher_data)
        return msg.model_dump_json()

    def unwrap_ws_message(self, raw: str) -> str:
        """Decrypt a WSCipherMessage JSON back to a plain string."""
        msg = WSCipherMessage.model_validate_json(raw)
        return self.decrypt(msg).decode()


# ---------------------------------------------------------------------------
# Public key resolution
# ---------------------------------------------------------------------------


def load_public_key(pem: str) -> RSAPublicKey:
    """Load an RSA public key from a PEM string."""
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, RSAPublicKey):
        raise ValueError("Expected RSA public key")
    return key


def fetch_public_key(base_url: str, api_key: str = "") -> RSAPublicKey | None:
    """Fetch the RSA public key from the LB's /api/dl/public-key endpoint."""
    url = base_url.rstrip("/") + "/api/dl/public-key"
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=5)
        if resp.status_code != 200:
            logger.warning("Failed to fetch public key: HTTP %d", resp.status_code)
            return None
        key = serialization.load_pem_public_key(resp.content)
        if not isinstance(key, RSAPublicKey):
            logger.warning("Fetched key is not RSA")
            return None
        return key
    except Exception:
        logger.warning("Failed to fetch public key from %s", url, exc_info=True)
        return None


def resolve_public_key(base_url: str, api_key: str = "") -> RSAPublicKey | None:
    """Resolve the public key: env var DL_PUBLIC_KEY_PEM first, then fetch from LB."""
    pem_env = os.environ.get("DL_PUBLIC_KEY_PEM", "").strip()
    if pem_env:
        try:
            return load_public_key(pem_env)
        except Exception:
            logger.warning("Invalid DL_PUBLIC_KEY_PEM env var", exc_info=True)

    if base_url:
        return fetch_public_key(base_url, api_key)

    return None
