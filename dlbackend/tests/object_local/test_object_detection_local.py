"""Tests for object detection endpoints using local server.

Starts a local TestClient (FastAPI lifespan loads models from config),
parametrizes tests across all known detectors, and skips any that
aren't ready.
"""

import base64
import json
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from dlserver.app import app

load_dotenv(override=True)

DL_API_KEY: str = os.getenv("DL_API_KEY", "")
FIXTURES_DIR: Path = Path(__file__).parent.parent / "fixtures" / "images"
AUTH_HEADERS: dict[str, str] = {"X-API-Key": DL_API_KEY}

ALL_DETECTORS: list[str] = ["yoloworld", "owlv2", "grounding-dino"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_image() -> tuple[str, int, int]:
    """Load the test image once. Returns (b64, width, height)."""
    img = cv2.imread(str(FIXTURES_DIR / "person_drinking.jpg"))
    h, w = img.shape[:2]
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    b64 = base64.b64encode(buf.tobytes()).decode()
    return b64, w, h


@pytest.fixture(scope="session")
def test_image_b64(test_image: tuple[str, int, int]) -> str:
    """Just the b64 string for tests that don't need dimensions."""
    return test_image[0]


@pytest.fixture(scope="session")
def random_frame_b64() -> str:
    """Generate a random noise frame once."""
    frame = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def client() -> TestClient:
    """Start local server — lifespan loads models from config."""
    return TestClient(app)


@pytest.fixture(scope="session")
def ready_detectors(client: TestClient) -> set[str]:
    """Query the models endpoint once and cache the set of ready names."""
    resp = client.get("/api/dl/object-detect/models", headers=AUTH_HEADERS)
    if resp.status_code != 200:
        return set()
    models: list[dict[str, Any]] = resp.json().get("models", [])
    return {m["name"] for m in models if m.get("ready")}


def _skip_if_not_ready(
    detector: str, ready_detectors: set[str],
) -> None:
    if detector not in ready_detectors:
        pytest.skip(f"Detector '{detector}' not ready")


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


class TestObjectDetectionHTTP:
    def test_list_models(self, client: TestClient) -> None:
        resp = client.get("/api/dl/object-detect/models", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "models" in body

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_compat_endpoint_returns_list(
        self, client: TestClient, detector: str,
        ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": test_image_b64, "classes": ["person", "chair"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_wrapped_endpoint_returns_detections(
        self, client: TestClient, detector: str,
        ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = client.post(
            f"/api/dl/object-detect/{detector}",
            json={"image_b64": test_image_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        assert "detections" in body
        assert isinstance(body["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detect_without_classes_uses_defaults(
        self, client: TestClient, detector: str,
        ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": test_image_b64},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_detection_item_fields(
        self, client: TestClient, detector: str,
        ready_detectors: set[str], test_image: tuple[str, int, int],
    ) -> None:
        """Detection items have class_name, xywh (pixel coords), and confidence."""
        _skip_if_not_ready(detector, ready_detectors)
        img_b64, img_w, img_h = test_image
        resp = client.post(
            f"/api/dl/{detector}",
            json={"image_b64": img_b64, "classes": ["person"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        detections: list[dict[str, Any]] = resp.json()
        for det in detections:
            assert "class_name" in det
            assert "xywh" in det
            assert "confidence" in det
            assert len(det["xywh"]) == 4
            x, y, w, h = det["xywh"]
            assert 0 <= x <= img_w, f"x={x} out of [0, {img_w}]"
            assert 0 <= y <= img_h, f"y={y} out of [0, {img_h}]"
            assert 0 < w <= img_w, f"w={w} out of (0, {img_w}]"
            assert 0 < h <= img_h, f"h={h} out of (0, {img_h}]"
            assert 0 < det["confidence"] <= 1.0

        assert any(d["class_name"] == "person" for d in detections), (
            f"No 'person' detected by {detector} on person_drinking.jpg"
        )

    def test_unknown_detector_returns_503(
        self, client: TestClient, random_frame_b64: str,
    ) -> None:
        resp = client.post(
            "/api/dl/nonexistent",
            json={"image_b64": random_frame_b64},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# WebSocket tests
# ---------------------------------------------------------------------------


class TestObjectDetectionWebSocket:
    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_heartbeat(
        self, client: TestClient, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
            resp: dict[str, Any] = ws.receive_json()
            assert resp == {"status": "ok"}

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_frame_returns_detections(
        self, client: TestClient, detector: str,
        ready_detectors: set[str], test_image_b64: str,
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({
                "type": "frame", "task": "object",
                "frame_b64": test_image_b64,
            }))
            resp: dict[str, Any] = ws.receive_json()
            assert "detections" in resp
            assert isinstance(resp["detections"], list)

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_config_update(
        self, client: TestClient, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({
                "type": "config", "task": "object",
                "classes": ["person", "dog"],
                "threshold": 0.5,
            }))
            resp: dict[str, Any] = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_unknown_detector_closes_ws(self, client: TestClient) -> None:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/api/dl/object-detection/nonexistent/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "heartbeat", "task": "object"}))
                ws.receive_json()

    @pytest.mark.parametrize("detector", ALL_DETECTORS)
    def test_invalid_json(
        self, client: TestClient, detector: str, ready_detectors: set[str],
    ) -> None:
        _skip_if_not_ready(detector, ready_detectors)
        with client.websocket_connect(
            f"/api/dl/object-detection/{detector}/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text("not json")
            resp: dict[str, Any] = ws.receive_json()
            assert "error" in resp
