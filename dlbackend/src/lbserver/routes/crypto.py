"""Crypto endpoints — public key distribution."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from lbserver.utils.state import get_crypto

router = APIRouter()


@router.get("/api/dl/public-key", response_class=PlainTextResponse)
async def get_public_key() -> str:
    """Return the server's RSA public key in PEM format."""
    crypto = get_crypto()
    if crypto is None:
        raise HTTPException(status_code=404, detail="Encryption not enabled")
    return crypto.public_key_pem
