"""
Sensing Service — background loop that detects motion/sound/faces/light and pushes events to Lamp Server.

Lamp Server (Go, port 5000) then forwards these events to OpenClaw via WebSocket chat.send,
so the AI agent can react proactively (Pillar 4: "It acts on its own").

Detectors:
  - Motion: camera frame differencing (grayscale → absdiff → threshold → contour area)
  - Face: InsightFace recognition — owner/stranger classification (presence.enter/leave)
  - Light level: mean brightness of camera frame (auto-adjust lamp)
  - Sound: RMS level from microphone (loud noise detection)

Also drives the PresenceService state machine for automatic light on/off.

Events are POST-ed to http://localhost:5000/api/sensing/event as:
  {"type": "motion", "message": "...", "image": "<base64 jpeg>"}
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import requests

import lelamp.config as config
from devices.video_capture_device import VideoCaptureDeviceBase
from lelamp.service.motors.animation_service import AnimationService
from lelamp.service.rgb.rgb_service import RGBService
from lelamp.service.sensing.perceptions.models import PerceptionConfig
from lelamp.service.sensing.perceptions.orchestrator import PerceptionOrchestrator
from lelamp.service.sensing.presence_service import PresenseService
from lelamp.service.voice.tts_service import TTSService

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:
    if TYPE_CHECKING:
        import cv2
    else:
        cv2 = None


try:
    import numpy as np
except ImportError:
    if TYPE_CHECKING:
        import numpy as np
    else:
        np = None

try:
    import sounddevice as sd
except ImportError:
    if TYPE_CHECKING:
        import sounddevice as sd
    else:
        sd = None


class SensingService:
    """Background sensing loop. Runs in a daemon thread."""

    # Settle time after freezing servos before capturing a frame (seconds)
    FREEZE_SETTLE_S: float = 0.3

    def __init__(
        self,
        camera_capture: VideoCaptureDeviceBase | None = None,
        input_device: int | str | None = None,
        poll_interval: float = 2.0,
        rgb_service: RGBService | None = None,
        tts_service: TTSService | None = None,
        animation_service: AnimationService | None = None,
        on_restore_aim: Callable[[], None] | None = None,
        is_sleeping: Callable[[], bool] | None = None,
    ):
        self._camera: VideoCaptureDeviceBase | None = camera_capture
        self._input_device: int | str | None = input_device
        self._poll_interval: float = poll_interval
        self._rgb_service: RGBService | None = rgb_service
        self._tts_service: TTSService | None = tts_service
        self._animation_service: AnimationService | None = animation_service
        self._on_restore_aim: Callable[[], None] | None = on_restore_aim
        self._is_sleeping: Callable[[], bool] | None = (
            is_sleeping  # callable → bool; suppresses non-wake events
        )

        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._last_event_time: dict[str, float] = {}

        self._perception_orchestrator: PerceptionOrchestrator = PerceptionOrchestrator(
            poll_interval_ts=self._poll_interval,
            send_event=self._send_event,
            perception_config=PerceptionConfig(
                enable_face=True,
                enable_motion=config.MOTION_ENABLED,
                enable_motion_per_face=config.MOTION_PER_FACE_ENABLED,
                enable_emotion=config.EMOTION_ENABLED,
                enable_pose=config.POSE_ENABLED,
                enable_light=True,
                enable_sound=True,
                enable_fire_hazard=config.FIRE_HAZARD_ENABLED,
            ),
        )

        # Presence auto on/off state machine
        self._presense_service: PresenseService = PresenseService(
            rgb_service=rgb_service,
            send_event=self._send_event,
            on_restore_aim=on_restore_aim,
        )
        _ = self._perception_orchestrator.with_presence_service(self._presense_service)

        if self._camera is not None:
            _ = self._perception_orchestrator.with_camera_service(self._camera)

        if self._tts_service is not None:
            _ = self._perception_orchestrator.with_tts_service(self._tts_service)

    def set_tts_service(self, tts_service: TTSService):
        """Set TTS reference after late initialization (echo suppression)."""
        _ = self._perception_orchestrator.with_tts_service(tts_service)

    @property
    def presence(self) -> PresenseService:
        return self._presense_service

    def start(self):
        if self._running:
            return
        self._running = True
        self._perception_orchestrator.start()

    def stop(self):
        self._running = False
        self._perception_orchestrator.stop()
        logger.info("SensingService stopped")

    # --- Frame encoding ---

    def _capture_stable_frame(self):
        """Freeze servos, wait for settle, capture a fresh frame, then unfreeze.

        Returns a camera frame suitable for _encode_frame(), or None on failure.
        Always freezes regardless of whether an animation is playing — a 0.3s
        pause is imperceptible but eliminates motion blur from the servo arm.
        """
        if not self._camera or not cv2:
            return None

        anim = self._animation_service
        if anim:
            anim.freeze()
            time.sleep(self.FREEZE_SETTLE_S)
        frame = self._camera.capture()
        if anim:
            anim.unfreeze()

        if frame is None:
            return None
        return frame.frame

    # --- Snapshot storage (two-tier) ---
    # Tmp: fast rotation buffer, lost on reboot
    _snapshot_tmp_paths: list[str] = []
    # Persist: survives reboot, agent can look back (TTL + size rotation)

    def _save_frame(self, prefix: str, frame: cv2.typing.MatLike) -> str | None:
        """Save a camera frame as a JPEG to the tmp snapshot dir at original resolution.

        Keeps at most SNAPSHOT_TMP_MAX_COUNT files; deletes the oldest when exceeded.
        Returns the saved file path, or None on failure.
        """
        try:
            os.makedirs(config.SNAPSHOT_TMP_DIR, exist_ok=True)
            tmp_dir = Path(config.SNAPSHOT_TMP_DIR)

            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

            filename = f"{int(time.time() * 1000)}.jpg"
            subdir = tmp_dir / f"sensing_{prefix}"
            subdir.mkdir(exist_ok=True, parents=True)
            filepath = subdir / filename
            with open(filepath, "wb") as f:
                _ = f.write(buf.tobytes())

            self._snapshot_tmp_paths.append(str(filepath))

            # Evict oldest files if over the limit
            while len(self._snapshot_tmp_paths) > config.SNAPSHOT_TMP_MAX_COUNT:
                oldest = self._snapshot_tmp_paths.pop(0)
                try:
                    os.remove(oldest)
                except OSError:
                    pass

            return str(filepath)
        except Exception as e:
            logger.debug("Frame save failed: %s", e)
            return None

    def _persist_snapshot(self, prefix: str, tmp_path: str) -> str | None:
        """Copy a tmp snapshot to the persistent dir with TTL + size rotation.

        Returns the persistent file path, or None on failure.
        """
        try:
            persist_dir = os.path.join(config.SNAPSHOT_PERSIST_DIR, f"sensing_{prefix}")
            os.makedirs(persist_dir, exist_ok=True)

            # Rotate: remove files older than TTL
            now = time.time()
            for f in os.listdir(persist_dir):
                fp = os.path.join(persist_dir, f)
                try:
                    if now - os.path.getmtime(fp) > config.SNAPSHOT_PERSIST_TTL_S:
                        os.remove(fp)
                except OSError:
                    pass

            # Rotate: if total size exceeds max, remove oldest files
            files = []
            for f in os.listdir(persist_dir):
                fp = os.path.join(persist_dir, f)
                try:
                    files.append((fp, os.path.getmtime(fp), os.path.getsize(fp)))
                except OSError:
                    pass
            files.sort(key=lambda x: x[1])  # oldest first
            total = sum(s for _, _, s in files)
            while total > config.SNAPSHOT_PERSIST_MAX_BYTES and files:
                oldest_path, _, oldest_size = files.pop(0)
                try:
                    os.remove(oldest_path)
                    total -= oldest_size
                except OSError:
                    pass

            # Copy snapshot to persistent dir
            dest = os.path.join(persist_dir, os.path.basename(tmp_path))

            _ = shutil.copy2(tmp_path, dest)
            return dest
        except Exception as e:
            logger.debug("Persist snapshot failed: %s", e)
            return None

    # --- Event sending ---

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        last_events = {k: int(now - v) for k, v in self._last_event_time.items()}
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "last_event_seconds_ago": last_events,
            "perceptions": self._perception_orchestrator.perceptions_state(),
            "presence": self._presense_service.to_dict(),
        }

    def _send_event(
        self,
        event_type: str,
        message: str,
        prefix: str = "",
        images: list[cv2.typing.MatLike] | None = None,
        cooldown: float | None = None,
    ):
        # Suppress sensing events while sleeping — only allow presence.enter to wake up
        if self._is_sleeping and self._is_sleeping() and event_type != "presence.enter":
            logger.debug("[sensing] sleeping — suppressed %s", event_type)
            return

        # New presence session — clear MotionPerception dedup so the next
        # motion.activity isn't silently dropped by the 5-min window.
        # Otherwise a friend arriving while someone was already sitting would
        # wait out the remainder of the old window before the agent saw them.
        #
        # Pass the *current* user so perceptions can skip the reset when the
        # visible user hasn't actually changed (e.g. stranger_79 → stranger_77,
        # both collapse to "unknown"). Without this guard, face-recognition
        # flicker between stranger IDs wipes the dedup every few seconds.
        if event_type == "presence.enter":
            self._perception_orchestrator.reset_dedup()

        cur_ts = time.time()
        # motion.activity has its own 5-min dedup in MotionPerception —
        # skip the global cooldown so different activities (drink vs
        # sedentary) are never silently dropped.
        if event_type not in ("motion.activity", "emotion.detected"):
            cd = cooldown if cooldown is not None else config.EVENT_COOLDOWN_S
            last = self._last_event_time.get(event_type, 0)
            if cur_ts - last < cd:
                return

        # Collect all images to save (single image or list)
        frames = images or []

        # Save each frame and append snapshot paths to the message.
        for frame in frames:
            tmp_path = self._save_frame(prefix, frame)
            if tmp_path:
                persist_path = self._persist_snapshot(prefix, tmp_path)
                ref = persist_path or tmp_path
                message = f"{message}\n[snapshot: {ref}]"

        logger.info("[sensing] %s: %s", event_type, message)

        payload: dict[str, object] = {"type": event_type, "message": message}
        # Include LeLamp's effective current_user so Lamp handler doesn't
        # have to re-derive it from the message text. Text parsing breaks
        # when a stranger-only enter event fires while a friend is still
        # present (extractUserName sees no friend in the message and
        # downgrades mood.CurrentUser() to "unknown", even though the
        # friend is still within forget window). LeLamp's current_user()
        # is the source of truth — ship it.
        try:
            cu = self._perception_orchestrator.current_user or ""
        except Exception:
            logger.exception("[sensing] face_recognizer.current_user() failed")
            cu = ""
        payload["current_user"] = cu
        logger.debug("[sensing] payload = %s", payload)

        try:
            resp = requests.post(
                config.LAMP_SENSING_URL,
                json=payload,
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(
                    "[sensing] Lamp returned %d: %s", resp.status_code, resp.text
                )
            else:
                self._last_event_time[event_type] = cur_ts
        except requests.RequestException as e:
            logger.warning("[sensing] Failed to send event to Lamp: %s", e)
