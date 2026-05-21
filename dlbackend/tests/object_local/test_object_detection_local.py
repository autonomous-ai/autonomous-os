"""Tests for object detection endpoints using local YOLO-World model."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.enums.object import ObjectDetectorEnum
from core.perception.object.perception import ObjectPerception
from core.perception.object.utils import ObjectDetectorFactory
from dlserver.utils.state import set_object_models

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

YOLO_WORLD_MODEL = "yolov8s-worldv2.pt"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "images"

pytestmark = pytest.mark.skipif(
    not Path(YOLO_WORLD_MODEL).exists() and not Path(f"local/{YOLO_WORLD_MODEL}").exists(),
    reason="YOLO-World model not found",
)


def _load_test_image_b64(name: str = "person_drinking.jpg") -> str:
    """Load a real test image as base64."""
    img = cv2.imread(str(FIXTURES_DIR / name))
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode()


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    """Load YOLO-World once for the entire test session."""
    model_path = Path(YOLO_WORLD_MODEL)
    if not model_path.exists():
        model_path = Path(f"local/{YOLO_WORLD_MODEL}")

    factory = ObjectDetectorFactory(
        model_name=ObjectDetectorEnum.YOLO_WORLD,
        model_path=model_path,
    )
    perception = ObjectPerception(object_detector_factory=factory)
    asyncio.run(perception.start())
    return perception


@pytest.fixture()
def client(model):
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_object_models({"yoloworld": model})
    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}
DETECTOR_NAME = "yoloworld"


class TestObjectDetectionHTTP:
    def test_compat_endpoint_returns_list(self, client):
        """Backward-compat flat-list endpoint (go2 format)."""
        resp = client.post(
            f"/api/dl/{DETECTOR_NAME}",
            json={"image_b64": _load_test_image_b64(), "classes": ["person", "chair", "table"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

    def test_wrapped_endpoint_returns_detections(self, client):
        resp = client.post(
            f"/api/dl/object-detect/{DETECTOR_NAME}",
            json={"image_b64": _load_test_image_b64(), "classes": ["person", "chair"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "detections" in body
        assert isinstance(body["detections"], list)

    def test_detect_without_classes_uses_defaults(self, client):
        resp = client.post(
            f"/api/dl/{DETECTOR_NAME}",
            json={"image_b64": _load_test_image_b64()},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_detection_item_fields(self, client):
        resp = client.post(
            f"/api/dl/{DETECTOR_NAME}",
            json={"image_b64": _load_test_image_b64("person_drinking.jpg"), "classes": ["person"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        for det in resp.json():
            assert "class_name" in det
            assert "xywh" in det
            assert "confidence" in det
            assert len(det["xywh"]) == 4
            assert isinstance(det["confidence"], float)

    def test_unknown_detector_returns_503(self, client):
        resp = client.post(
            "/api/dl/nonexistent",
            json={"image_b64": _make_frame_b64()},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 503

    def test_list_models(self, client):
        resp = client.get("/api/dl/object-detect/models", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert any(m["name"] == DETECTOR_NAME for m in body["models"])


class TestObjectDetectionWebSocket:
    def test_heartbeat(self, client):
        with client.websocket_connect(
            f"/api/dl/object-detection/{DETECTOR_NAME}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

    def test_frame_returns_detections(self, client):
        with client.websocket_connect(
            f"/api/dl/object-detection/{DETECTOR_NAME}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({
                "type": "frame", "task": "object",
                "frame_b64": _load_test_image_b64(),
            }))
            resp = ws.receive_json()
            assert "detections" in resp
            assert isinstance(resp["detections"], list)

    def test_config_update(self, client):
        with client.websocket_connect(
            f"/api/dl/object-detection/{DETECTOR_NAME}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({
                "type": "config", "task": "object",
                "classes": ["person", "dog"],
                "threshold": 0.5,
            }))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_unknown_detector_closes_ws(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/api/dl/object-detection/nonexistent/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
                ws.receive_json()

    def test_invalid_json(self, client):
        with client.websocket_connect(
            f"/api/dl/object-detection/{DETECTOR_NAME}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text("not json")
            resp = ws.receive_json()
            assert "error" in resp
