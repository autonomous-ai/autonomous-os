"""Tests for ergonomic assessment through the pose estimation endpoint."""

import asyncio
import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.models.pose import PosePerceptionSessionConfig
from core.perception.pose.perception import PosePerception
from core.perception.pose.utils import ErgoAssessorFactory, PoseEstimator2DFactory
from dlserver.utils.state import get_pose_model, set_pose_model

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

RTMPOSE_MODEL_PATH = Path.cwd() / "local" / "rtmpose-m.onnx"

pytestmark = pytest.mark.skipif(
    not RTMPOSE_MODEL_PATH.exists(),
    reason=f"Local RTMPose model not found at {RTMPOSE_MODEL_PATH}",
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    """Load RTMPose 2D + RULA ergo assessor."""
    from core.enums.pose import ErgoAssessorEnum, PoseEstimator2DEnum

    estimator_2d_factory = PoseEstimator2DFactory(
        model_name=PoseEstimator2DEnum.RTMPOSE, model_path=RTMPOSE_MODEL_PATH
    )
    ergo_assessor_factory = ErgoAssessorFactory(
        model_name=ErgoAssessorEnum.RULA,
        confidence_threshold=0.0,  # low threshold so random frames produce results
    )
    pose_model = PosePerception(
        estimator_2d_factory=estimator_2d_factory,
        ergo_assessor_factory=ergo_assessor_factory,
        default_config=PosePerceptionSessionConfig(
            confidence_threshold_2d=0.0,
            min_valid_keypoints=0,
        ),
    )
    asyncio.run(pose_model.start())
    return pose_model


@pytest.fixture()
def client(model):
    import config
    import server

    config.settings.dl_api_key = TEST_API_KEY
    set_pose_model(model)
    return TestClient(server.app)


AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


class TestErgoViaWebSocket:
    def test_frame_returns_ergo(self, client):
        """With ergo assessor configured, response should include ergo field."""
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "pose_2d" in resp
            assert "ergo" in resp

    def test_ergo_has_expected_fields(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            ergo = resp["ergo"]
            assert "score" in ergo
            assert "risk_level" in ergo
            assert "left" in ergo
            assert "right" in ergo

    def test_ergo_side_has_body_scores(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            for side_key in ("left", "right"):
                side = resp["ergo"][side_key]
                assert "score" in side
                assert "risk_level" in side
                assert "body_scores" in side
                assert "skipped_joints" in side
                assert "upper_arm" in side["body_scores"]
                assert "neck" in side["body_scores"]
                assert "trunk" in side["body_scores"]

    def test_ergo_score_range(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert 1 <= resp["ergo"]["score"] <= 7
            assert 1 <= resp["ergo"]["left"]["score"] <= 7
            assert 1 <= resp["ergo"]["right"]["score"] <= 7

    def test_ergo_overall_is_max_of_sides(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert resp["ergo"]["score"] == max(
                resp["ergo"]["left"]["score"], resp["ergo"]["right"]["score"]
            )

    def test_multiple_frames_all_have_ergo(self, client):
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            for _ in range(3):
                ws.send_text(
                    json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
                )
                resp = ws.receive_json()
                assert "ergo" in resp


class TestNoErgoWithoutAssessor:
    def test_no_ergo_when_not_configured(self):
        """When no ergo assessor is configured, ergo should not appear."""
        from core.enums.pose import PoseEstimator2DEnum

        factory = PoseEstimator2DFactory(
            model_name=PoseEstimator2DEnum.RTMPOSE, model_path=RTMPOSE_MODEL_PATH
        )
        pose_model_no_ergo = PosePerception(estimator_2d_factory=factory)
        asyncio.run(pose_model_no_ergo.start())

        import config
        import server

        saved = get_pose_model()
        config.settings.dl_api_key = TEST_API_KEY
        set_pose_model(pose_model_no_ergo)
        test_client = TestClient(server.app)

        with test_client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()
            assert "pose_2d" in resp
            assert "ergo" not in resp

        set_pose_model(saved)
        asyncio.run(pose_model_no_ergo.stop())
