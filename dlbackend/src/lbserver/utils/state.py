"""Shared state for the load balancer."""

from core.crypto.rsa_aes import RSAAESCrypto

_crypto: RSAAESCrypto | None = None


def get_crypto() -> RSAAESCrypto | None:
    return _crypto


def set_crypto(crypto: RSAAESCrypto | None) -> None:
    global _crypto
    _crypto = crypto
