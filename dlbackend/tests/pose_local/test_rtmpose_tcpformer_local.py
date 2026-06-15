"""Tests for pose estimation with 2D RTMPose + 3D TCPFormer lifting."""

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
from core.perception.pose.utils import PoseEstimator2DFactory, PoseLifter3DFactory
from dlserver.utils.state import set_pose_model

TEST_API_KEY = "test-secret-key"
os.environ["DL_API_KEY"] = TEST_API_KEY

RTMPOSE_MODEL_PATH = Path.cwd() / "local" / "rtmpose-m.onnx"
TCPFORMER_MODEL_PATH = Path.cwd() / "local" / "tcpformer_h36m_243.onnx"

pytestmark = pytest.mark.skipif(
    not RTMPOSE_MODEL_PATH.exists() or not TCPFORMER_MODEL_PATH.exists(),
    reason=f"Local models not found (rtmpose={RTMPOSE_MODEL_PATH.exists()}, tcpformer={TCPFORMER_MODEL_PATH.exists()})",
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="session")
def model():
    """Load RTMPose 2D + TCPFormer 3D once for the entire test session."""
    from core.enums.pose import PoseEstimator2DEnum, PoseLifter3DEnum

    estimator_2d_factory = PoseEstimator2DFactory(
        model_name=PoseEstimator2DEnum.RTMPOSE, model_path=RTMPOSE_MODEL_PATH
    )
    lifter_3d_factory = PoseLifter3DFactory(
        model_name=PoseLifter3DEnum.TCPFORMER,
        model_path=TCPFORMER_MODEL_PATH,
        input_size=(320, 240),
    )
    pose_model = PosePerception(
        estimator_2d_factory=estimator_2d_factory,
        lifter_3d_factory=lifter_3d_factory,
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


class TestPoseWith3DLifting:
    def test_ws_returns_pose_2d_and_3d(self, client):
        """A single frame should produce both 2D and 3D output."""
        with client.websocket_connect(
            "/hal/api/dl/pose-estimation/ws", headers=AUTH_HEADERS
        ) as ws:
            ws.send_text(
                json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()})
            )
            resp = ws.receive_json()

            # 2D
            assert resp["pose_2d"]["graph_type"] == "coco"
            assert len(resp["pose_2d"]["joints"]) == 17

            # 3D
            pose_3d = resp["pose_3d"]
            assert pose_3d["graph_type"] == "h36m"
            assert len(pose_3d["joints"]) == 17
            assert len(pose_3d["confs"]) == 17
            for joint in pose_3d["joints"]:
                assert len(joint) == 3
                assert all(isinstance(v, float) for v in joint)
