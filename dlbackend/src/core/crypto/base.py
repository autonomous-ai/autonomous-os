"""Abstract base class for crypto operations."""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

ENCRYPT_T = TypeVar("ENCRYPT_T")
DECRYPT_T = TypeVar("DECRYPT_T")


class CryptoBase(Generic[ENCRYPT_T, DECRYPT_T], ABC):
    """Base interface for encryption/decryption."""

    @abstractmethod
    def encrypt(self, payload: DECRYPT_T) -> ENCRYPT_T:
        pass

    @abstractmethod
    def decrypt(self, payload: ENCRYPT_T) -> DECRYPT_T:
        pass
