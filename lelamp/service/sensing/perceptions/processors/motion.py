import base64
import json
import logging
import os
import threading
import time
from copy import copy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, override

import cv2
import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

import lelamp.config as config
from lelamp.service.sensing.crypto import CryptoSession, WSKeyExchangeRequest, resolve_public_key
from lelamp.service.sensing.perceptions.typing import SendEventCallable
from lelamp.service.sensing.perceptions.utils import (
    PerceptionStateObservers,
    record_dl_stall,
)
from lelamp.service.sensing.presence_service import PresenceState, PresenseService

from .base import Perception
from .pose import PosePerception

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

RESOURCES_DIR = Path(__file__).parent / "resources"

# Map raw Kinetics action labels to high-level activity groups.
# Lamp receives the raw labels — the agent infers the group. The mapping here
# is kept only to filter out emotional actions (handled by a separate channel).
ACTIVITY_GROUP: dict[str, str] = {
    # drink — reset hydration timer
    "drinking": "drink",
    "drinking beer": "drink",
    "drinking shots": "drink",
    "tasting beer": "drink",
    "opening bottle": "drink",
    "making tea": "drink",
    # break — reset break timer (stretching, movement, social)
    "stretching arm": "break",
    "stretching leg": "break",
    "applauding": "break",
    "clapping": "break",
    "celebrating": "break",
    "sneezing": "break",
    "sniffing": "break",
    "hugging": "break",
    "kissing": "break",
    "headbanging": "break",
    "sticking tongue out": "break",
    # eat — meal signal (raw labels kept for phrasing + per-food UI icons)
    "tasting food": "eat",
    "dining": "eat",
    "eating burger": "eat",
    "eating cake": "eat",
    "eating carrots": "eat",
    "eating chips": "eat",
    "eating doughnuts": "eat",
    "eating hotdog": "eat",
    "eating ice cream": "eat",
    "eating spaghetti": "eat",
    "eating watermelon": "eat",
    # sedentary — create wellbeing/music crons if missing
    "using computer": "sedentary",
    "writing": "sedentary",
    "texting": "sedentary",
    "reading book": "sedentary",
    "reading newspaper": "sedentary",
    "drawing": "sedentary",
    "playing controller": "sedentary",
    # emotional — always speak, log mood
    "laughing": "emotional",
    "crying": "emotional",
    "yawning": "emotional",
    "singing": "emotional",
}


class MoveEnum(Enum):
    BACKGROUND = (
        "background"  # whole scene shifting — camera shake or very close object
    )
    FOREGROUND = "foreground"  # localized movement — person walking, object moving
    NONE = "none"


@dataclass
class MotionDetection:
    class_name: str
    conf: float


@dataclass
class ActionResponse:
    detected_classes: list[MotionDetection]


class RemoteMotionChecker:
    """Video action recognition-based motion detector."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        whitelist: list[str] | None = None,
        threshold: float = config.MOTION_CONFIDENCE_THRESHOLD,
        person_detection_enabled: bool = config.MOTION_PERSON_DETECTION_ENABLED,
        person_min_area_ratio: float = config.MOTION_PERSON_MIN_AREA_RATIO,
    ):
        self._base_url: str = base_url
        self._api_key: str = api_key
        self._whitelist: list[str] | None = whitelist
        self._threshold: float = threshold
        self._person_detection_enabled: bool = person_detection_enabled
        self._person_min_area_ratio: float = person_min_area_ratio
        self._ws_session: ClientConnection | None = None
        self._crypto: CryptoSession | None = None

        self._prepare_session()

        self._last_action: str | None = None
        self._last_heartbeat_ts: float = 0.0
        self._heartbeat_interval: float = config.DL_HEARTBEAT_INTERVAL_S

    def _prepare_session(self):
        if self._ws_session is not None:
            logger.info("[%s] has been started", self.__class__.__name__)
            return

        try:
            ws_url = self._base_url.replace("http", "ws").replace("https", "wss")
            logger.info("[%s] connecting to %s", self.__class__.__name__, ws_url)
            self._ws_session = connect(
                ws_url,
                additional_headers={"X-API-Key": self._api_key},
                open_timeout=config.DL_WS_RECV_TIMEOUT_S,
            )
            self._crypto = None
            if config.DL_ENCRYPTION_ENABLED:
                self._setup_crypto()

            config_msg = json.dumps(
                {
                    "type": "config",
                    "task": "action",
                    "whitelist": self._whitelist,
                    "threshold": self._threshold,
                    "person_detection_enabled": self._person_detection_enabled,
                    "person_min_area_ratio": self._person_min_area_ratio,
                }
            )
            if self._crypto is not None:
                config_msg = self._crypto.wrap_ws_message(config_msg)
            self._ws_session.send(config_msg)
            # Consume the config_updated response
            raw = self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S)
            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)
        except Exception:
            logger.exception("Failed to connect to remote motion recognition backend")
            self._ws_session = None

    def _setup_crypto(self) -> None:
        """Perform WS key exchange after connection.

        Raises RuntimeError if DL_ENCRYPTION_REQUIRED and setup fails.
        """
        if self._ws_session is None:
            raise RuntimeError("Cannot setup crypto without a WS connection")

        public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
        if public_key is None:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")
            logger.warning("[%s] encryption enabled but no public key — plaintext fallback", self.__class__.__name__)
            return

        session = CryptoSession(public_key)
        key_req = WSKeyExchangeRequest(encrypted_key=session.encrypted_key_b64)
        self._ws_session.send(key_req.model_dump_json())
        resp = json.loads(self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S))
        if resp.get("status") == "key_exchange_ok":
            self._crypto = session
            logger.info("[%s] encryption session established", self.__class__.__name__)
        else:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError(f"Key exchange failed: {resp}")
            logger.warning("[%s] key exchange failed: %s — plaintext fallback", self.__class__.__name__, resp)

    def _img2b64(self, frame: cv2.typing.MatLike):
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def _send_heartbeat(self) -> None:
        """Send a heartbeat if the interval has elapsed."""
        now = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return

        self._last_heartbeat_ts = now

        if self._ws_session is None:
            return
        try:
            msg = json.dumps({"type": "heartbeat", "task": "action"})
            if self._crypto is not None:
                msg = self._crypto.wrap_ws_message(msg)
            self._ws_session.send(msg)
            raw = self._ws_session.recv()
            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)
            resp = json.loads(raw)
            if resp.get("status") == "ok":
                logger.debug("[motion] heartbeat ok")
            else:
                logger.warning("[motion] heartbeat unexpected response: %s", resp)
        except (ConnectionClosed, TimeoutError):
            logger.warning("[motion] heartbeat failed — connection lost")
            self._ws_session = None

    def update(self, frame: cv2.typing.MatLike) -> list[MotionDetection] | None:
        """Send a frame for action recognition inference.

        Returns list of dicts with keys: class_name, conf.
        Sorted by confidence descending. Returns None if unavailable,
        [] if nothing passes the backend threshold.
        """

        # Auto-reconnect if session was lost
        if self._ws_session is None:
            self._prepare_session()
            if self._ws_session is not None:
                logger.info(
                    "[%s] reconnected to %s", self.__class__.__name__, self._base_url
                )

        self._send_heartbeat()

        if self._ws_session is not None:
            try:
                msg = json.dumps(
                    {
                        "type": "frame",
                        "task": "action",
                        "frame_b64": self._img2b64(frame),
                    }
                )
                if self._crypto is not None:
                    msg = self._crypto.wrap_ws_message(msg)

                self._ws_session.send(msg)
                raw = self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S)

                if self._crypto is not None:
                    raw = self._crypto.unwrap_ws_message(raw)

                resp = json.loads(raw)
                detected_classes = sorted(
                    resp.get("detected_classes", []),
                    key=lambda x: x["conf"],
                    reverse=True,
                )
                return [
                    MotionDetection(class_name=dc["class_name"], conf=dc["conf"])
                    for dc in detected_classes
                ]
            except TimeoutError:
                # Backend accepted the frame but never replied within the
                # budget. Drop the session so the worker is freed immediately
                # instead of blocking forever and starving other perceptions.
                logger.warning(
                    "[%s] recv timeout (%.1fs) — dropping session, retry next tick",
                    self.__class__.__name__,
                    config.DL_WS_RECV_TIMEOUT_S,
                )
                record_dl_stall(
                    "motion", f"frame recv timeout {config.DL_WS_RECV_TIMEOUT_S:.0f}s"
                )
                self._ws_session = None
            except ConnectionClosed:
                logger.warning(
                    "[%s] connection lost, will retry on next tick",
                    self.__class__.__name__,
                )
                self._ws_session = None

        return None

    def ready(self):
        return self._ws_session is not None

    def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws_session is not None:
            try:
                self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None

    @property
    def last_action(self) -> str | None:
        return self._last_action


class MotionPerception(Perception[cv2.typing.MatLike]):
    """Detects motion via remote DL backend action recognition.

    Snapshots are buffered and flushed every MOTION_FLUSH_S seconds,
    sending all accumulated snapshots together in one event.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        base_url: str = config.DL_MOTION_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)
        self._presense_service: PresenseService | None = presense_service
        self._last_motion_time: float | None = None

        whitelist = self._load_whitelist()

        self._checker: RemoteMotionChecker = RemoteMotionChecker(
            base_url=base_url,
            api_key=api_key,
            whitelist=whitelist,
            threshold=config.MOTION_CONFIDENCE_THRESHOLD,
        )

        # Snapshot buffer — flushed every MOTION_FLUSH_S
        self._flush_interval: float = config.MOTION_FLUSH_S
        self._last_flush_ts: float = 0.0
        self._snapshot_paths: list[str] = []
        self._snapshots_buffer: list[cv2.typing.MatLike] = []
        self._actions_buffer: list[str] = []

        # Dedup state for outbound motion.activity events.
        # Key = (current_user, frozenset(labels)) where `labels` matches what
        # actually goes into the message: bucket names for drink/break, raw
        # Kinetics labels for sedentary + eat. So `writing → drawing` flips
        # the key (sedentary stays raw) and passes through so the agent sees
        # the new activity; same logic now applies to `eating burger →
        # eating cake` (eat stays raw, distinct keys), trading a bit of
        # extra noise for richer reaction phrasing. Same key within
        # MOTION_DEDUP_WINDOW_S = drop (saves Lamp tokens). User change flips
        # the key immediately; different strangers collapse to "unknown" so
        # they don't break dedup on their own.
        self._last_sent_key: tuple[str, frozenset[str]] | None = None
        self._last_sent_ts: float = 0.0
        self._dedup_window_s: float = 300.0  # 5 min

        self._state_lock: threading.RLock = threading.RLock()

        # Sedentary streak — tracks how long the user has been in a
        # continuous "sedentary" activity (using computer / writing / …).
        # Wired in by the orchestrator. Used to fold posture_summary into
        # motion.activity whenever pose's tumbling window completes.
        self._pose_perception: PosePerception | None = None
        # Tracks when the current continuous-sedentary stretch began. Used
        # only to compute the [computer_streak_min: N] context hint that
        # rides alongside the posture summary — not a gate.
        self._sedentary_streak_start_ts: float = 0.0

    def set_pose_perception(self, pose: PosePerception | None) -> None:
        """Wire in the pose sampler so motion can fold posture summaries
        into outbound activity events. Called by the orchestrator after both
        perceptions are constructed."""
        self._pose_perception = pose

    @staticmethod
    def _load_whitelist() -> list[str] | None:
        whitelist_path = RESOURCES_DIR / "white_list.txt"
        if not whitelist_path.exists():
            logger.warning("[motion] whitelist file not found: %s", whitelist_path)
            return None
        lines = whitelist_path.read_text().strip().splitlines()
        whitelist = [line.strip() for line in lines if line.strip()]
        logger.info("[motion] loaded %d whitelist entries", len(whitelist))
        return whitelist

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        frame = data
        if frame is None:
            logger.debug("[motion] frame is None, skipping")
            return

        try:
            detections = self._checker.update(frame)
        except Exception:
            logger.exception("[motion] inference error")
            return

        if detections:
            logger.debug("[motion] detections: %s", [d.class_name for d in detections])
        else:
            logger.debug("[motion] no detections")

        with self._state_lock:
            if detections:
                self._last_motion_time = time.time()
                if self._presense_service is not None:
                    self._presense_service.on_motion()

                self._snapshots_buffer.append(frame)
                self._actions_buffer.extend([d.class_name for d in detections])

                # Save annotated snapshot
                snapshot_path = self._save_annotated(frame, detections)
                if snapshot_path:
                    self._snapshot_paths.append(snapshot_path)

            self._flush_buffer()

    def _draw_annotations(
        self, frame: cv2.typing.MatLike, detections: list[MotionDetection]
    ) -> cv2.typing.MatLike:
        """Draw detected action labels on a copy of the frame."""
        vis = frame.copy()
        y_offset = 30
        for det in detections:
            label = f"{det.class_name} ({det.conf:.2f})"
            _ = cv2.putText(
                vis,
                label,
                (10, y_offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
            )
            y_offset += 30
        return vis

    def _save_annotated(
        self, frame: cv2.typing.MatLike, detections: list[MotionDetection]
    ) -> str | None:
        """Draw annotations and save to snapshot dir. Rotates old files."""
        try:
            os.makedirs(config.MOTION_SNAPSHOT_DIR, exist_ok=True)

            annotated = self._draw_annotations(frame, detections)
            filename = f"motion_{int(time.time() * 1000)}.jpg"
            filepath = os.path.join(config.MOTION_SNAPSHOT_DIR, filename)
            _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
            with open(filepath, "wb") as f:
                _ = f.write(buf.tobytes())

            # Rotate: remove oldest files if over max count
            files = sorted(
                (
                    os.path.join(config.MOTION_SNAPSHOT_DIR, f)
                    for f in os.listdir(config.MOTION_SNAPSHOT_DIR)
                    if f.endswith(".jpg")
                ),
                key=os.path.getmtime,
            )
            while len(files) > config.MOTION_SNAPSHOT_MAX_COUNT:
                try:
                    os.remove(files.pop(0))
                except OSError:
                    pass

            return filepath
        except Exception as e:
            logger.debug("[motion] snapshot save failed: %s", e)
            return None

    def _flush_buffer(self) -> None:
        with self._state_lock:
            if not self._snapshots_buffer:
                return

            cur_ts = time.time()
            if (cur_ts - self._last_flush_ts) < self._flush_interval:
                return

            actions = copy(self._actions_buffer)
            snapshots_buffer = copy(self._snapshots_buffer)
            self._snapshots_buffer.clear()
            self._snapshot_paths.clear()
            self._actions_buffer.clear()
            self._last_flush_ts = cur_ts

        # Log raw detections in this flush window — useful for tuning
        # the whitelist / ACTIVITY_GROUP mapping and for diagnosing why a
        # particular flush did/didn't produce an event.
        if actions:
            logger.info("[motion] raw actions in window: %s", actions)

        # Hybrid output: drink/break collapse to bucket name, sedentary + eat
        # keep the raw Kinetics label. Bucket names are enough for hydration
        # and break timer resets — the agent doesn't need the specific drink
        # or movement type. Sedentary keeps the raw label so the agent can
        # ground nudge phrasing and music-genre choice in the concrete
        # activity (writing / reading book / playing controller / …). Eat
        # keeps the raw label so reaction phrasing can reference the actual
        # food (burger / dining / spaghetti / …) and the per-food UI icons
        # render.
        labels: set[str] = set()

        for a in reversed(actions):
            group = ACTIVITY_GROUP.get(a)
            if group is None:
                logger.warning("[motion] unmapped action '%s', skipping", a)
                continue
            if group == "emotional":
                # Emotional actions (laughing/crying/yawning/singing) are
                # intentionally NOT emitted via motion.activity. A dedicated
                # motion.emotional event will be added later to carry them;
                # until then emotional detections are silently ignored
                # here so motion.activity stays purely about physical actions.
                continue
            if group in ("sedentary", "eat"):
                labels.add(a)
            else:
                labels.add(group)

        if not labels:
            return

        if (
            self._presense_service is not None
            and self._presense_service.state != PresenceState.PRESENT
        ):
            logger.info(
                "[motion] skipping event — no presence (presence=%s)",
                self._presense_service.state,
            )
            return

        # Track sedentary streak: time the user has been in continuous
        # static activity. Starts on the first sedentary flush, stays warm
        # while subsequent flushes still contain a sedentary label, resets
        # the moment the activity transitions to something non-sedentary.
        has_sedentary: bool = any(
            ACTIVITY_GROUP.get(label) == "sedentary" for label in labels
        )
        if has_sedentary:
            if self._sedentary_streak_start_ts <= 0:
                self._sedentary_streak_start_ts = cur_ts
            # Sedentary is the SOLE trigger to open the pose tumbling
            # window. Idempotent — subsequent sedentary flushes inside an
            # already-open window are no-ops. Once the window is open it
            # runs purely on POSE_WINDOW_DURATION_S; later stretch breaks
            # don't stop the clock, they just leave the bad_ratio honest.
            if self._pose_perception is not None:
                self._pose_perception.start_window()
        else:
            if self._sedentary_streak_start_ts > 0:
                logger.debug(
                    "[motion] sedentary streak ended after %.1f min (labels=%s)",
                    (cur_ts - self._sedentary_streak_start_ts) / 60.0,
                    sorted(labels),
                )
            self._sedentary_streak_start_ts = 0.0

        message = f"Activity detected: {', '.join(sorted(labels))}."

        # Posture tumbling-window evaluation. Pose.py opens a window on the
        # first sedentary flush. Once it's been open for POSE_WINDOW_DURATION_S,
        # we evaluate the aggregate and ALWAYS reset — fire or no-fire.
        # The window itself is the rhythm: no separate streak gate (window
        # start = "user is sedentary now") and no separate cooldown (next
        # fire is naturally one window away). Only two gates remain at fold
        # time: bad_ratio over the configured threshold, and the user must
        # still be sedentary on this flush (don't nag mid-stretch).
        posture_injected: bool = False
        if (
            self._pose_perception is not None
            and self._pose_perception.is_window_complete()
        ):
            if has_sedentary and self._sedentary_streak_start_ts > 0:
                streak_s: float = cur_ts - self._sedentary_streak_start_ts
                streak_min: int = int(streak_s / 60)
                summary: dict[str, Any] | None = (
                    self._pose_perception.get_posture_summary()
                )
                if summary is not None and summary["bad_ratio"] >= config.POSE_BAD_RATIO:
                    summary_with_streak: dict[str, Any] = dict(summary)
                    summary_with_streak["streak_min"] = streak_min
                    # Bucket pointers are surfaced as separate markers (not
                    # inside posture_summary) so the Lamp handler can lift
                    # them off the message before stripping for the LLM —
                    # the agent never sees the file paths. Mirrors the
                    # existing [snapshot: …] marker pattern.
                    bucket_id: str = str(summary.get("bucket_id", "") or "")
                    worst_snaps: list[str] = list(summary.get("worst_snapshots") or [])
                    # Don't ride bucket info inside the LLM-facing summary
                    # JSON either — pop it so the agent sees only the
                    # posture stats it actually reasons about.
                    summary_with_streak.pop("bucket_id", None)
                    summary_with_streak.pop("worst_snapshots", None)
                    message = (
                        f"{message}\n"
                        f"[computer_streak_min: {streak_min}]\n"
                        f"[posture_summary: "
                        f"{json.dumps(summary_with_streak, separators=(',', ':'))}]"
                    )
                    if bucket_id:
                        message = f"{message}\n[pose_bucket: {bucket_id}]"
                    if worst_snaps:
                        message = (
                            f"{message}\n[pose_worst: "
                            f"{','.join(worst_snaps)}]"
                        )
                    posture_injected = True
                    logger.info(
                        "[motion] folding posture summary "
                        "(streak=%dm bad_ratio=%.2f dominant=%s samples=%d bucket=%s worst=%d)",
                        streak_min,
                        summary["bad_ratio"],
                        summary["dominant_region"],
                        summary["samples"],
                        bucket_id,
                        len(worst_snaps),
                    )
            # Unconditional reset — a completed window with no fire (stretch
            # break in progress, bad_ratio under threshold, or too few samples
            # for the noise floor) still must clear, otherwise the next
            # sit-down would evaluate stale data from the previous cycle.
            self._pose_perception.reset_window()

        # Dedup: drop if the outbound state (user + outbound labels) hasn't
        # changed since the last send AND we're still within the dedup window.
        # A user change or a label-set change flips the key — those always
        # pass through. After 5 min the same key passes through anyway so
        # Lamp agent wakes up and reruns the threshold check.
        current_user = self._perception_state.current_user.data or ""

        key = (current_user, frozenset(labels))
        if (
            self._last_sent_key == key
            and (cur_ts - self._last_sent_ts) < self._dedup_window_s
            and not posture_injected
        ):
            logger.info(
                "[motion] dedup drop: %s (same as last send %.1fs ago)",
                message,
                cur_ts - self._last_sent_ts,
            )
            return
        if posture_injected and self._last_sent_key == key and (cur_ts - self._last_sent_ts) < self._dedup_window_s:
            logger.info(
                "[motion] dedup BYPASS (posture nudge): would-have-dropped (last send %.1fs ago)",
                cur_ts - self._last_sent_ts,
            )
        self._last_sent_key = key
        self._last_sent_ts = cur_ts

        # Log each outbound label to Lamp wellbeing BEFORE firing the event.
        # Log-first means when the agent reads history on motion.activity,
        # the new rows are already there — no read-before-write race if the
        # skill queries concurrently. Log-and-forget on failure: we keep the
        # same semantics as the old agent-side POST (a missing row is just a
        # missing row; skills tolerate gaps).
        self._post_wellbeing_labels(current_user, labels)

        # Attach latest snapshot path

        logger.info("[motion] flushing: %s", message)

        self._send_event("motion.activity", message, "motion_activity",[snapshots_buffer[-1]], None)

    def _post_wellbeing_labels(self, user: str, labels: set[str]) -> None:
        """POST each activity label to Lamp wellbeing log.

        Replaces the agent's per-label POST that used to live in the
        wellbeing SKILL (Step 1). Fires synchronously but with a short
        timeout so a stuck Lamp never blocks motion detection.
        """
        log_user = user or "unknown"
        for label in sorted(labels):
            try:
                resp = requests.post(
                    config.LAMP_WELLBEING_LOG_URL,
                    json={"action": label, "notes": "", "user": log_user},
                    timeout=2,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[motion] wellbeing log %s returned %d: %s",
                        label,
                        resp.status_code,
                        resp.text,
                    )
            except requests.RequestException as e:
                logger.debug("[motion] wellbeing log %s failed: %s", label, e)

    @override
    def cleanup(self) -> None:
        self._checker.close()

    def reset_dedup(self, new_user: str = "") -> None:
        """Clear the outbound dedup state only if the visible user actually
        changed. Called by SensingService on presence.enter — without this
        guard, every stranger flicker (stranger_79 → stranger_77, both
        collapsing to "unknown" via FaceRecognizer.current_user()) would wipe
        the key and bypass the 5-minute window, spamming motion.activity
        events on every presence.enter. Resetting only on an actual user
        transition (leo → unknown, unknown → chloe, chloe → leo) keeps the
        dedup window honest while still letting a new presence session see a
        fresh activity event immediately.
        """
        if self._last_sent_key is None:
            return
        last_user = self._last_sent_key[0]
        if last_user == new_user:
            logger.debug(
                "[motion] dedup reset skipped — same user %r",
                last_user,
            )
            return
        logger.info(
            "[motion] dedup reset (user %r → %r)",
            last_user,
            new_user,
        )
        self._last_sent_key = None
        self._last_sent_ts = 0.0
        self._sedentary_streak_start_ts = 0.0

    def to_dict(self) -> dict[str, Any]:
        seconds_since = (
            int(time.time() - self._last_motion_time)
            if self._last_motion_time is not None
            else None
        )
        last_key = self._last_sent_key
        return {
            "type": "motion",
            "connected": self._checker.ready(),
            "last_raw_actions": sorted(last_key[1]) if last_key else [],
            "last_user": last_key[0] if last_key else None,
            "buffered_snapshots": len(self._snapshots_buffer),
            "motion_detected": self._last_motion_time is not None,
            "seconds_since_motion": seconds_since,
        }
