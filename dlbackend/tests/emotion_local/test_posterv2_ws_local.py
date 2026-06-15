"""Tests for the emotion-analysis WebSocket endpoint using the local POSTER V2 model."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.perception.face.utils import FaceDetectorFactory
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.perception.facial_emotion.perception import EmotionPerception
from core.perception.facial_emotion.utils import EmotionRecognizerFactory
from dlserver.utils.state import get_emotion_model, set_emotion_model

POSTERV2_EMOTIONS: list[str] = (
    (RESOURCES_DIR / "posterv2_classes.txt").read_text().strip().split("\n")
)

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY
os.environ["EMOTION_RECOGNITION_MODEL"] = "posterv2"

POSTERV2_MODEL_PATH = Path.cwd() / "local" / "posterv2_7cls.onnx"

pytestmark = pytest.mark.skipif(
    not POSTERV2_MODEL_PATH.exists(),
    reason=f"Local POSTER V2 model not found at {POSTERV2_MODEL_PATH}",
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _make_face_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG with a synthetic face-like region."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center = (width // 2, height // 2)
    axes = (50, 65)
    cv2.ellipse(frame, center, axes, 0, 0, 360, (200, 180, 170), -1)
    cv2.circle(frame, (center[0] - 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.circle(frame, (center[0] + 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.ellipse(frame, (center[0], center[1] + 25), (15, 8), 0, 0, 180, (40, 40, 80), -1)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    from core.enums import EmotionRecognizerEnum
    from core.enums.face import FaceDetectorEnum

    emotion_factory = EmotionRecognizerFactory(
        model_name=EmotionRecognizerEnum.POSTERV2, model_path=POSTERV2_MODEL_PATH
    )
    face_factory = FaceDetectorFactory(model_name=FaceDetectorEnum.YUNET)
    m = EmotionPerception(
        emotion_recognizer_factory=emotion_factory, face_detector_factory=face_factory
    )
    asyncio.run(m.start())
    return m


@pytest.fixture()
def client(model):
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_emotion_model(model)
    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


class TestHealthEndpoint:
    def test_health_reports_emotion_model(self, client):
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["models"]["emotion"] is True

    def test_health_emotion_not_loaded(self, client):

        saved = get_emotion_model()
        set_emotion_model(None)
        resp = client.get("/hal/api/dl/health", headers=AUTH_HEADERS)
        assert resp.json()["models"]["emotion"] is False
        set_emotion_model(saved)


class TestEmotionAnalysisWebSocket:
    def test_frame_returns_detections(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "detections" in resp
            assert isinstance(resp["detections"], list)

    def test_frame_with_face_returns_emotion_fields(self, client):
        """When a face is detected, each detection has the expected fields."""
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "frame", "task": "emotion", "frame_b64": _make_face_frame_b64()}
                )
            )
            resp = ws.receive_json()
            assert "detections" in resp
            for det in resp["detections"]:
                assert "emotion" in det
                assert "confidence" in det
                assert "face_confidence" in det
                assert "bbox" in det
                assert det["emotion"] in POSTERV2_EMOTIONS
                assert 0.0 <= det["confidence"] <= 1.0
                assert len(det["bbox"]) == 4
                # POSTER V2 does not output valence/arousal — should be None
                assert det.get("valence") is None
                assert det.get("arousal") is None

    def test_multiple_frames(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            for _ in range(3):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
                )
                resp = ws.receive_json()
                assert "detections" in resp

    def test_config_update_threshold(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "config", "task": "emotion", "threshold": 0.8}))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

    def test_high_threshold_filters_detections(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "config", "task": "emotion", "threshold": 1.0}))
            resp = ws.receive_json()
            assert resp["status"] == "config_updated"

            ws.send_text(
                json.dumps(
                    {"type": "frame", "task": "emotion", "frame_b64": _make_face_frame_b64()}
                )
            )
            resp = ws.receive_json()
            assert resp["detections"] == []

    def test_invalid_json(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text("not json at all")
            resp = ws.receive_json()
            assert "error" in resp

    def test_missing_type_field(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"frame_b64": "abc"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_frame_missing_frame_b64(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "frame", "task": "emotion"}))
            resp = ws.receive_json()
            assert "error" in resp

    def test_model_not_loaded_closes_ws(self, client):

        saved = get_emotion_model()
        set_emotion_model(None)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
            ) as ws:
                ws.send_text(json.dumps({"type": "frame", "task": "emotion", "frame_b64": "abc"}))
                ws.receive_json()
        set_emotion_model(saved)

    def test_heartbeat_returns_ok(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "task": "emotion"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

    def test_heartbeat_multiple(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            for _ in range(3):
                ws.send_text(json.dumps({"type": "heartbeat", "task": "emotion"}))
                resp = ws.receive_json()
                assert resp == {"status": "ok"}

    def test_heartbeat_interleaved_with_frames(self, client):
        with client.websocket_connect(
            "/hal/api/dl/emotion-analysis/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
            )
            ws.receive_json()

            ws.send_text(json.dumps({"type": "heartbeat", "task": "emotion"}))
            resp = ws.receive_json()
            assert resp == {"status": "ok"}

            ws.send_text(
                json.dumps({"type": "frame", "task": "emotion", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "detections" in resp

    def test_ws_without_api_key_rejected(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/hal/api/dl/emotion-analysis/ws") as ws:
                ws.send_text(json.dumps({"type": "config", "task": "emotion", "threshold": 0.5}))
                ws.receive_json()
