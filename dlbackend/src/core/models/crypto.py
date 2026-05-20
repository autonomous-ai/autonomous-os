"""Crypto payload dataclasses — raw bytes, no serialization."""

from dataclasses import dataclass


@dataclass
class AESGCMCipherPayload:
    """Encrypted data — output of encrypt, input of decrypt."""

    cipher_data: bytes
    nonce: bytes


@dataclass
class AESGCMPlainPayload:
    """Plain data — output of decrypt, input of encrypt."""

    plain_data: bytes


@dataclass
class RSAAESCipherPayload:
    """Encrypted data — output of encrypt, input of decrypt."""

    encrypted_key: bytes
    cipher_data: bytes
    nonce: bytes


@dataclass
class RSAAESPlainPayload:
    """Plain data — output of decrypt, input of encrypt."""

    encrypted_key: bytes
    plain_data: bytes
