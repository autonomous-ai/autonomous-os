"""Shared utilities for protocol handlers."""

import base64
import secrets

import cv2
import cv2.typing as cv2t
import numpy as np
from fastapi import HTTPException, WebSocket

from config import settings


def decode_image(image_b64: str) -> cv2t.MatLike:
    """Decode a base64-encoded JPEG/PNG image to a BGR numpy array."""
    try:
        img_bytes = base64.b64decode(image_b64)
        img_array = np.frombuffer(img_bytes, dtype=np.uint8)
        image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("cv2.imdecode returned None")
        return image
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to decode image: {e}")


async def verify_ws_api_key(websocket: WebSocket) -> bool:
    """Validate API key on WebSocket connect.

    Returns True if the key is valid (or no key is configured).
    Returns False and closes the connection if the key is invalid.
    """
    if settings.dl_api_key:
        api_key = websocket.headers.get("x-api-key", "")
        if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
            await websocket.close(code=1008, reason="Invalid or missing API key")
            return False
    return True
