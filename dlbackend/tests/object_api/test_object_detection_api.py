"""Integration tests against a remote DL backend server (object detection).

Requires DL_BACKEND_URL and DL_API_KEY in .env (or environment).
Run with: pytest tests/object_api/test_object_detection_api.py -v
"""

import base64
import json
import os
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import pytest_asyncio
import websockets
from dotenv import load_dotenv

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "")
DL_API_KEY = os.getenv("DL_API_KEY", "")
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "images"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping remote API tests"
)


def _load_test_image_b64(name: str = "person_drinking.jpg") -> str:
    img = cv2.imread(str(FIXTURES_DIR / name))
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode()


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS = {"X-API-Key": DL_API_KEY}
DETECTOR_NAME = "yoloworld"


class TestObjectDetectionHTTPCompat:
    """Backward-compatible flat-list endpoints (go2 format: /api/dl/{name})."""

    def test_yoloworld_returns_list(self):
        resp = httpx.post(
            _http_url("/api/dl/yoloworld"),
            json={"image_b64": _load_test_image_b64(), "classes": ["person", "chair"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_owlv2_returns_list(self):
        resp = httpx.post(
            _http_url("/api/dl/owlv2"),
            json={"image_b64": _load_test_image_b64(), "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code in (200, 503)

    def test_grounding_dino_returns_list(self):
        resp = httpx.post(
            _http_url("/api/dl/grounding-dino"),
            json={"image_b64": _load_test_image_b64(), "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code in (200, 503)

    def test_detect_with_real_image(self):
        resp = httpx.post(
            _http_url(f"/api/dl/{DETECTOR_NAME}"),
            json={"image_b64": _load_test_image_b64("person_drinking.jpg"), "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        for det in resp.json():
            assert "class_name" in det
            assert "xywh" in det
            assert "confidence" in det
            assert len(det["xywh"]) == 4

    def test_detect_without_classes(self):
        resp = httpx.post(
            _http_url(f"/api/dl/{DETECTOR_NAME}"),
            json={"image_b64": _load_test_image_b64()},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_health_includes_detectors(self):
        resp = httpx.get(
            _http_url("/api/dl/object-detect/models"),
            headers=AUTH_HEADERS,
            timeout=10,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body


class TestObjectDetectionHTTPWrapped:
    def test_detect_returns_detections(self):
        resp = httpx.post(
            _http_url(f"/api/dl/object-detect/{DETECTOR_NAME}"),
            json={"image_b64": _load_test_image_b64(), "classes": ["person"]},
            headers=AUTH_HEADERS,
            timeout=30,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "detections" in body
        assert isinstance(body["detections"], list)


class TestObjectDetectionWebSocket:
    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url(f"/api/dl/object-detection/{DETECTOR_NAME}/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_heartbeat(self, ws):
        await ws.send(json.dumps({"type": "heartbeat", "task": "object"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_frame_returns_detections(self, ws):
        await ws.send(json.dumps({
            "type": "frame", "task": "object",
            "frame_b64": _load_test_image_b64(),
        }))
        resp = json.loads(await ws.recv())
        assert "detections" in resp
        assert isinstance(resp["detections"], list)

    @pytest.mark.asyncio
    async def test_config_update(self, ws):
        await ws.send(json.dumps({
            "type": "config", "task": "object",
            "classes": ["person", "dog"],
            "threshold": 0.5,
        }))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

    @pytest.mark.asyncio
    async def test_invalid_json(self, ws):
        await ws.send("not json")
        resp = json.loads(await ws.recv())
        assert "error" in resp
