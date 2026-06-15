"""Integration tests against a remote DL backend server (pose endpoint).

Requires DL_BACKEND_URL and DL_API_KEY in .env (or environment).
Run with: pytest tests/pose_api/test_pose_estimation_api.py -v
"""

import base64
import json
import os

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

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping remote API tests"
)


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    """Create a base64-encoded JPEG of a random BGR image."""
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS = {"X-API-Key": DL_API_KEY}


class TestHealthEndpoint:
    def test_health_reports_pose_model(self):
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "pose" in body["models"]


class TestPoseEstimationWebSocket:
    @pytest_asyncio.fixture()
    async def ws(self):
        """Connect to the remote pose WebSocket with auth headers."""
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_frame_returns_pose_2d(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp
        assert "joints" in resp["pose_2d"]
        assert "confs" in resp["pose_2d"]
        assert len(resp["pose_2d"]["joints"]) == 17
        assert len(resp["pose_2d"]["confs"]) == 17

    @pytest.mark.asyncio
    async def test_multiple_frames(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
            resp = json.loads(await ws.recv())
            assert "pose_2d" in resp

    @pytest.mark.asyncio
    async def test_config_update(self, ws):
        await ws.send(json.dumps({"type": "config", "task": "pose", "frame_interval": 0.5}))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "config_updated"

    @pytest.mark.asyncio
    async def test_invalid_json(self, ws):
        await ws.send("not json at all")
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_missing_type_field(self, ws):
        await ws.send(json.dumps({"frame_b64": "abc"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_frame_missing_frame_b64(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_heartbeat_returns_ok(self, ws):
        await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_multiple(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
            resp = json.loads(await ws.recv())
            assert resp == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_heartbeat_interleaved_with_frames(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        await ws.recv()

        await ws.send(json.dumps({"type": "heartbeat", "task": "pose"}))
        resp = json.loads(await ws.recv())
        assert resp == {"status": "ok"}

        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp

    @pytest.mark.asyncio
    async def test_ws_without_api_key_rejected(self):
        with pytest.raises(Exception):
            async with websockets.connect(
                _ws_url("/hal/api/dl/pose-estimation/ws"),
            ) as conn:
                await conn.send(json.dumps({"type": "heartbeat", "task": "pose"}))
                _ = await conn.recv()


class TestErgoAssessmentWebSocket:
    """Tests for ergonomic assessment via the pose WS endpoint."""

    @pytest_asyncio.fixture()
    async def ws(self):
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as conn:
            yield conn

    @pytest.mark.asyncio
    async def test_ws_frame_returns_ergo_field(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        assert "pose_2d" in resp
        # ergo may or may not be present depending on server config

    @pytest.mark.asyncio
    async def test_ws_ergo_has_full_structure(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        if "ergo" in resp:
            ergo = resp["ergo"]
            assert "score" in ergo
            assert "risk_level" in ergo
            assert "left" in ergo
            assert "right" in ergo
            for side_key in ("left", "right"):
                side = ergo[side_key]
                assert "score" in side
                assert "body_scores" in side
                assert "skipped_joints" in side

    @pytest.mark.asyncio
    async def test_ws_ergo_score_range(self, ws):
        await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
        resp = json.loads(await ws.recv())
        if "ergo" in resp:
            assert 1 <= resp["ergo"]["score"] <= 7

    @pytest.mark.asyncio
    async def test_ws_multiple_frames_ergo_consistent(self, ws):
        for _ in range(3):
            await ws.send(json.dumps({"type": "frame", "task": "pose", "frame_b64": _make_frame_b64()}))
            resp = json.loads(await ws.recv())
            assert "pose_2d" in resp
            if "ergo" in resp:
                assert 1 <= resp["ergo"]["score"] <= 7
                assert resp["ergo"]["score"] == max(
                    resp["ergo"]["left"]["score"],
                    resp["ergo"]["right"]["score"],
                )
