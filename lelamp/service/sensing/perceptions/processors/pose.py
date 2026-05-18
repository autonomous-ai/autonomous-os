"""Pose estimation + ergonomic sampling via dlbackend WS.

Follows the same pattern as MotionPerception (RemoteMotionChecker):
- Maintains a WS connection to dlbackend /api/dl/pose-estimation/ws
- Sends camera frames, receives pose_2d + optional pose_3d + optional ergo
- Silently samples each frame into a rolling RAM buffer + daily JSONL file.
- Does NOT emit a pose.ergo_risk event directly. MotionPerception queries
  get_posture_summary() and folds the aggregate into motion.activity when
  the user is "using computer" for long enough.
"""

import base64
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, override

import cv2
import numpy as np
import numpy.typing as npt
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

import lelamp.config as config
from lelamp.service.sensing.perceptions.typing import SendEventCallable
from lelamp.service.sensing.perceptions.utils import PerceptionStateObservers
from lelamp.service.sensing.presence_service import PresenceState, PresenseService

from .base import Perception

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# COCO 17-joint skeleton for visualization
# ---------------------------------------------------------------------------

_COCO_SKELETON: list[tuple[int, int]] = [
    (15, 13), (13, 11), (16, 14), (14, 12),
    (11, 12), (5, 11), (6, 12), (5, 6),
    (5, 7), (6, 8), (7, 9), (8, 10),
    (1, 2), (0, 1), (0, 2), (1, 3), (2, 4), (3, 5), (4, 6),
]

# Bone colors: left side = cyan, right side = orange, center = green
_BONE_COLORS: list[tuple[int, int, int]] = [
    (255, 200, 0), (255, 200, 0),           # left leg
    (0, 100, 255), (0, 100, 255),           # right leg
    (0, 220, 0),                             # hip bridge
    (255, 200, 0), (0, 100, 255),           # hip to shoulder
    (0, 220, 0),                             # shoulder bridge
    (255, 200, 0), (0, 100, 255),           # shoulders to elbows
    (255, 200, 0), (0, 100, 255),           # elbows to wrists
    (0, 220, 0), (0, 220, 0), (0, 220, 0), # nose to eyes
    (255, 200, 0), (0, 100, 255),           # eyes to ears
    (255, 200, 0), (0, 100, 255),           # ears to shoulders
]

_RISK_COLORS: dict[int, tuple[int, int, int]] = {
    1: (0, 200, 0),     # negligible — green
    2: (0, 200, 200),   # low — yellow
    3: (0, 140, 255),   # medium — orange
    4: (0, 0, 255),     # high — red
}

_CONF_THRESHOLD: float = 0.3


def _draw_pose_2d(
    frame: cv2.typing.MatLike,
    pose_2d: dict[str, Any],
    ergo: dict[str, Any] | None = None,
) -> cv2.typing.MatLike:
    """Draw 2D skeleton and optional ergo score on frame. Returns a copy."""
    vis: npt.NDArray[np.uint8] = frame.copy()
    joints: list[list[float]] = pose_2d.get("joints", [])
    confs: list[float] = pose_2d.get("confs", [])

    if not joints:
        return vis

    kps: npt.NDArray[np.int32] = np.array(joints, dtype=np.int32)

    # Draw bones
    for idx, (u, v) in enumerate(_COCO_SKELETON):
        if max(u, v) >= len(kps):
            continue
        if confs[u] < _CONF_THRESHOLD or confs[v] < _CONF_THRESHOLD:
            continue
        color: tuple[int, int, int] = _BONE_COLORS[idx] if idx < len(_BONE_COLORS) else (0, 220, 0)
        cv2.line(vis, tuple(kps[u]), tuple(kps[v]), color, 2)

    # Draw joints
    for i, kp in enumerate(kps):
        if confs[i] < _CONF_THRESHOLD:
            continue
        cv2.circle(vis, tuple(kp), 4, (255, 255, 255), -1)

    # Draw ergo score label
    if ergo is not None:
        score: int = ergo.get("score", 0)
        risk_level: int = ergo.get("risk_level", 0)
        risk_names: dict[int, str] = {1: "negligible", 2: "low", 3: "medium", 4: "high"}
        label: str = f"RULA: {score} ({risk_names.get(risk_level, '?')})"
        color = _RISK_COLORS.get(risk_level, (200, 200, 200))
        cv2.putText(vis, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    return vis


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PoseResult:
    pose_2d: dict[str, Any] | None = None
    pose_3d: dict[str, Any] | None = None
    ergo: dict[str, Any] | None = None


@dataclass
class _PoseSample:
    """One posture snapshot recorded into the rolling buffer."""

    ts: float
    score: int
    risk_level: int
    region_max: dict[str, int] = field(default_factory=dict)
    noisy: bool = False
    raw_left: dict[str, Any] = field(default_factory=dict)
    raw_right: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Remote WS client (same pattern as RemoteMotionChecker)
# ---------------------------------------------------------------------------


class RemotePoseEstimator:
    """WS client to dlbackend /api/dl/pose-estimation/ws."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
    ):
        self._base_url: str = base_url
        self._api_key: str = api_key
        self._ws_session: ClientConnection | None = None
        self._last_heartbeat_ts: float = 0.0
        self._heartbeat_interval: float = config.DL_HEARTBEAT_INTERVAL_S

        self._prepare_session()

    def _prepare_session(self) -> None:
        if self._ws_session is not None:
            return

        try:
            ws_url: str = self._base_url.replace("http", "ws").replace("https", "wss")
            logger.info("[%s] connecting to %s", self.__class__.__name__, ws_url)
            self._ws_session = connect(
                ws_url, additional_headers={"X-API-Key": self._api_key}
            )
        except Exception:
            logger.exception("[%s] failed to connect", self.__class__.__name__)
            self._ws_session = None

    def _img2b64(self, frame: cv2.typing.MatLike) -> str:
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def _send_heartbeat(self) -> None:
        now: float = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return
        self._last_heartbeat_ts = now

        if self._ws_session is None:
            return
        try:
            self._ws_session.send(json.dumps({"type": "heartbeat", "task": "pose"}))
            resp: dict = json.loads(self._ws_session.recv())
            if resp.get("status") == "ok":
                logger.debug("[pose] heartbeat ok")
            else:
                logger.warning("[pose] heartbeat unexpected: %s", resp)
        except ConnectionClosed:
            logger.warning("[pose] heartbeat failed — connection lost")
            self._ws_session = None

    def update(self, frame: cv2.typing.MatLike) -> PoseResult | None:
        """Send a frame and return the pose result, or None if unavailable."""
        if self._ws_session is None:
            self._prepare_session()
            if self._ws_session is not None:
                logger.info("[%s] reconnected", self.__class__.__name__)

        self._send_heartbeat()

        if self._ws_session is None:
            return None

        try:
            self._ws_session.send(
                json.dumps(
                    {
                        "type": "frame",
                        "task": "pose",
                        "frame_b64": self._img2b64(frame),
                    }
                )
            )
            resp: dict = json.loads(self._ws_session.recv())

            if "error" in resp:
                logger.warning("[pose] backend error: %s", resp["error"])
                return None

            return PoseResult(
                pose_2d=resp.get("pose_2d"),
                pose_3d=resp.get("pose_3d"),
                ergo=resp.get("ergo"),
            )
        except ConnectionClosed:
            logger.warning(
                "[%s] connection lost, will retry on next tick", self.__class__.__name__
            )
            self._ws_session = None
            return None

    def ready(self) -> bool:
        return self._ws_session is not None

    def close(self) -> None:
        if self._ws_session is not None:
            try:
                self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None


# ---------------------------------------------------------------------------
# Perception processor
# ---------------------------------------------------------------------------


_REGIONS: tuple[str, ...] = ("neck", "trunk", "upper_arm", "lower_arm", "wrist")
_ANGLE_KEYS: tuple[str, ...] = (
    "upper_arm_angle",
    "lower_arm_angle",
    "neck_angle",
    "trunk_angle",
)


class PosePerception(Perception[cv2.typing.MatLike]):
    """Pose estimation + silent ergonomic sampling.

    Each tick:
    1. Send the frame to dlbackend pose-estimation WS.
    2. While the user is present, append one sample per
       POSE_SAMPLE_INTERVAL_S to a rolling RAM deque AND a daily JSONL file.
    3. NEVER emit an event directly — MotionPerception calls
       get_posture_summary() and decides whether to fold it into the next
       motion.activity payload.

    Single-frame noise (wrap-edge ±180°, transient reaches for a cup) is
    filtered at aggregation time, not at the sample tap.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None,
        base_url: str = config.DL_POSE_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)
        self._presence_service: PresenseService | None = presense_service
        self._estimator: RemotePoseEstimator = RemotePoseEstimator(
            base_url=base_url,
            api_key=api_key,
        )
        self._last_result: PoseResult | None = None
        self._risk_threshold: int = config.POSE_ERGO_HIGH_RISK_THRESHOLD
        self._samples: deque[_PoseSample] = deque(
            maxlen=config.POSE_WINDOW_SAMPLES
        )
        self._last_sample_ts: float = 0.0
        self._samples_dir: str = os.path.join(
            config.SNAPSHOT_TMP_DIR, "sensing_pose"
        )
        os.makedirs(self._samples_dir, exist_ok=True)

    @staticmethod
    def _frame_noisy(
        body_left: dict[str, Any], body_right: dict[str, Any]
    ) -> bool:
        threshold: float = config.POSE_NOISY_ANGLE_THRESHOLD
        for body in (body_left, body_right):
            for key in _ANGLE_KEYS:
                angle: Any = body.get(key)
                if angle is None:
                    continue
                try:
                    if abs(float(angle)) >= threshold:
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    @staticmethod
    def _region_max(
        body_left: dict[str, Any], body_right: dict[str, Any]
    ) -> dict[str, int]:
        out: dict[str, int] = {}
        for region in _REGIONS:
            try:
                left_val: int = int(body_left.get(region, 0))
            except (TypeError, ValueError):
                left_val = 0
            try:
                right_val: int = int(body_right.get(region, 0))
            except (TypeError, ValueError):
                right_val = 0
            out[region] = max(left_val, right_val)
        return out

    def _samples_file_path(self, ts: float) -> str:
        day: str = time.strftime("%Y-%m-%d", time.localtime(ts))
        return os.path.join(self._samples_dir, f"samples_{day}.jsonl")

    def _append_sample_file(self, sample: _PoseSample) -> None:
        payload: dict[str, Any] = {
            "ts": round(sample.ts, 2),
            "score": sample.score,
            "risk_level": sample.risk_level,
            "region_max": sample.region_max,
            "noisy": sample.noisy,
        }
        try:
            path: str = self._samples_file_path(sample.ts)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as e:
            logger.debug("[pose.sample] file append failed: %s", e)

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        if data is None:
            return

        result: PoseResult | None = self._estimator.update(data)
        if result is None:
            return

        self._last_result = result

        ergo: dict[str, Any] | None = result.ergo
        if ergo is None:
            return

        now: float = time.time()

        # Presence gate: if the user isn't here, clear the buffer (session
        # ended) and skip sampling.
        if (
            self._presence_service is not None
            and self._presence_service.state != PresenceState.PRESENT
        ):
            if self._samples:
                logger.debug(
                    "[pose.sample] presence lost — buffer reset (had %d)",
                    len(self._samples),
                )
                self._samples.clear()
                self._last_sample_ts = 0.0
            return

        # Throttle to one sample per POSE_SAMPLE_INTERVAL_S regardless of
        # the underlying tick rate.
        if now - self._last_sample_ts < config.POSE_SAMPLE_INTERVAL_S:
            return

        left: dict[str, Any] = ergo.get("left", {}) or {}
        right: dict[str, Any] = ergo.get("right", {}) or {}
        body_left: dict[str, Any] = left.get("body_scores", {}) or {}
        body_right: dict[str, Any] = right.get("body_scores", {}) or {}

        sample = _PoseSample(
            ts=now,
            score=int(ergo.get("score", 0) or 0),
            risk_level=int(ergo.get("risk_level", 0) or 0),
            region_max=self._region_max(body_left, body_right),
            noisy=self._frame_noisy(body_left, body_right),
            raw_left=dict(left),
            raw_right=dict(right),
        )
        self._samples.append(sample)
        self._last_sample_ts = now
        self._append_sample_file(sample)
        logger.debug(
            "[pose.sample] ts=%.0f score=%d risk=%d noisy=%s buffer=%d/%d",
            now,
            sample.score,
            sample.risk_level,
            sample.noisy,
            len(self._samples),
            config.POSE_WINDOW_SAMPLES,
        )

    def get_posture_summary(self) -> dict[str, Any] | None:
        """Aggregate the rolling buffer into a summary dict.

        Returns None when the buffer hasn't filled yet or when more than half
        of the samples are flagged noisy (low-confidence window).
        """
        window: int = config.POSE_WINDOW_SAMPLES
        if len(self._samples) < window:
            return None

        valid: list[_PoseSample] = [s for s in self._samples if not s.noisy]
        if len(valid) < window // 2:
            return None

        bad: list[_PoseSample] = [s for s in valid if s.risk_level >= 3]
        bad_ratio: float = len(bad) / len(valid)

        region_freq: dict[str, int] = {region: 0 for region in _REGIONS}
        for s in bad:
            for region, sub in s.region_max.items():
                if sub >= 3:
                    region_freq[region] = region_freq.get(region, 0) + 1

        dominant_region: str = ""
        dominant_count: int = 0
        if bad:
            dominant_region = max(region_freq, key=lambda r: region_freq[r])
            dominant_count = region_freq[dominant_region]

        latest: _PoseSample = self._samples[-1]
        window_min: int = int(
            window * config.POSE_SAMPLE_INTERVAL_S / 60
        )
        return {
            "bad_ratio": round(bad_ratio, 2),
            "valid_samples": len(valid),
            "bad_samples": len(bad),
            "window_min": window_min,
            "region_frequency": region_freq,
            "dominant_region": dominant_region,
            "dominant_count": dominant_count,
            "latest_score": latest.score,
            "latest_risk_level": latest.risk_level,
            "latest_left": latest.raw_left,
            "latest_right": latest.raw_right,
        }

    def is_window_bad(self) -> bool:
        """True when the gate criteria are met (window full AND bad ratio over threshold)."""
        summary: dict[str, Any] | None = self.get_posture_summary()
        if summary is None:
            return False
        return summary["bad_ratio"] >= config.POSE_BAD_RATIO

    def draw_latest_overlay(
        self, frame: cv2.typing.MatLike
    ) -> cv2.typing.MatLike:
        """Annotate a frame with the most recent pose result (for snapshots)."""
        if self._last_result is None or self._last_result.pose_2d is None:
            return frame
        return _draw_pose_2d(
            frame, self._last_result.pose_2d, self._last_result.ergo
        )

    @override
    def cleanup(self) -> None:
        self._estimator.close()

    def to_dict(self) -> dict[str, Any]:
        ergo_score: int | None = None
        ergo_risk: int | None = None
        has_pose_2d: bool = False
        has_pose_3d: bool = False

        if self._last_result is not None:
            has_pose_2d = self._last_result.pose_2d is not None
            has_pose_3d = self._last_result.pose_3d is not None
            if self._last_result.ergo is not None:
                ergo_score = self._last_result.ergo.get("score")
                ergo_risk = self._last_result.ergo.get("risk_level")

        samples_until_gate: int = max(
            0, config.POSE_WINDOW_SAMPLES - len(self._samples)
        )
        seconds_since_sample: float | None = None
        if self._last_sample_ts > 0:
            seconds_since_sample = time.time() - self._last_sample_ts

        return {
            "type": "pose",
            "connected": self._estimator.ready(),
            "has_pose_2d": has_pose_2d,
            "has_pose_3d": has_pose_3d,
            "ergo_score": ergo_score,
            "ergo_risk_level": ergo_risk,
            "seconds_since_sample": int(seconds_since_sample)
            if seconds_since_sample is not None
            else None,
            "samples_in_buffer": len(self._samples),
            "samples_until_gate": samples_until_gate,
            "window_samples": config.POSE_WINDOW_SAMPLES,
            "sample_interval_s": config.POSE_SAMPLE_INTERVAL_S,
            "bad_ratio_threshold": config.POSE_BAD_RATIO,
            "summary": self.get_posture_summary(),
            "samples": [
                {
                    "ts": round(s.ts, 2),
                    "score": s.score,
                    "risk_level": s.risk_level,
                    "region_max": s.region_max,
                    "noisy": s.noisy,
                }
                for s in self._samples
            ],
        }
