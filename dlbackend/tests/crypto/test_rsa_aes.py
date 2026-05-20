"""Tests for RSA + AES-256-GCM hybrid encryption."""

import os

import pytest
from cryptography.exceptions import InvalidTag

from core.crypto.rsa_aes import AESGCMSession, RSAAESCrypto
from core.models.crypto import AESGCMCipherPayload, AESGCMPlainPayload, RSAAESCipherPayload, RSAAESPlainPayload


class TestAESGCMSession:
    def test_encrypt_decrypt_round_trip(self):
        key = os.urandom(32)
        session = AESGCMSession(key)
        original = b"hello world"

        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=original))
        decrypted = session.decrypt(encrypted)
        assert decrypted.plain_data == original

    def test_different_nonce_per_encrypt(self):
        key = os.urandom(32)
        session = AESGCMSession(key)
        payload = AESGCMPlainPayload(plain_data=b"same data")

        enc1 = session.encrypt(payload)
        enc2 = session.encrypt(payload)
        assert enc1.nonce != enc2.nonce
        assert enc1.cipher_data != enc2.cipher_data

    def test_tampered_ciphertext_fails(self):
        key = os.urandom(32)
        session = AESGCMSession(key)
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=b"secret"))

        tampered = AESGCMCipherPayload(
            cipher_data=encrypted.cipher_data + b"\x00",
            nonce=encrypted.nonce,
        )
        with pytest.raises(InvalidTag):
            session.decrypt(tampered)

    def test_wrong_key_fails(self):
        key1 = os.urandom(32)
        key2 = os.urandom(32)
        session1 = AESGCMSession(key1)
        session2 = AESGCMSession(key2)

        encrypted = session1.encrypt(AESGCMPlainPayload(plain_data=b"secret"))
        with pytest.raises(InvalidTag):
            session2.decrypt(encrypted)

    def test_large_payload(self):
        key = os.urandom(32)
        session = AESGCMSession(key)
        original = os.urandom(1_000_000)

        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=original))
        decrypted = session.decrypt(encrypted)
        assert decrypted.plain_data == original


class TestRSAAESCrypto:
    def _encrypt_session_key(self, crypto: RSAAESCrypto, session_key: bytes) -> bytes:
        """Helper: RSA-encrypt a session key using the crypto's public key."""
        return crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)

    def test_create_session_round_trip(self):
        crypto = RSAAESCrypto()
        session_key = os.urandom(32)
        encrypted_key = self._encrypt_session_key(crypto, session_key)

        session = crypto.create_session(encrypted_key)
        original = b"test data"
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=original))
        decrypted = session.decrypt(encrypted)
        assert decrypted.plain_data == original

    def test_encrypt_decrypt_round_trip(self):
        crypto = RSAAESCrypto()
        session_key = os.urandom(32)
        encrypted_key = self._encrypt_session_key(crypto, session_key)

        original = b"hello from hybrid"
        encrypted = crypto.encrypt(RSAAESPlainPayload(encrypted_key=encrypted_key, plain_data=original))
        decrypted = crypto.decrypt(encrypted)
        assert decrypted.plain_data == original

    def test_public_key_pem(self):
        crypto = RSAAESCrypto()
        pem = crypto.public_key_pem
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")

    def test_keys_persisted_to_disk(self, tmp_path):
        key_dir = tmp_path / "keys"
        crypto1 = RSAAESCrypto(key_dir=key_dir)
        pem1 = crypto1.public_key_pem

        assert (key_dir / "private_key.pem").exists()
        assert (key_dir / "public_key.pem").exists()

        crypto2 = RSAAESCrypto(key_dir=key_dir)
        assert crypto2.public_key_pem == pem1

    def test_in_memory_keys_are_unique(self):
        crypto1 = RSAAESCrypto()
        crypto2 = RSAAESCrypto()
        assert crypto1.public_key_pem != crypto2.public_key_pem

    def test_wrong_encrypted_key_fails(self):
        crypto = RSAAESCrypto()
        session_key = os.urandom(32)
        encrypted_key = self._encrypt_session_key(crypto, session_key)

        original = b"secret"
        encrypted = crypto.encrypt(RSAAESPlainPayload(encrypted_key=encrypted_key, plain_data=original))

        other_key = os.urandom(32)
        other_encrypted_key = self._encrypt_session_key(crypto, other_key)
        tampered = RSAAESCipherPayload(
            encrypted_key=other_encrypted_key,
            cipher_data=encrypted.cipher_data,
            nonce=encrypted.nonce,
        )
        with pytest.raises(InvalidTag):
            crypto.decrypt(tampered)
