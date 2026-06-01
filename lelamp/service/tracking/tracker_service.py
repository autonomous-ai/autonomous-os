"""Vision-guided object tracking with servo follow — gimbal hybrid mode.

Workflow:
  1. Caller provides a target label (or bbox). YOLO finds the object in the
     current frame and initialises a CSRT local tracker.
  2. A fast loop (FAST_LOOP_FPS) updates CSRT each frame, computes the pixel
     offset from frame center, and applies EMA smoothing before nudging servos.
  3. A background YOLO thread fires every YOLO_REDETECT_S to correct tracker
     drift — it does NOT block the fast loop (non-freezing, queue-based).
  4. If CSRT loses the object YOLO_MAX_MISS times in a row, tracking stops.
"""

import base64
import json
import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np
import numpy.typing as npt
import requests

import lelamp.config as config
from lelamp.config import (
    TRACKING_DETECT_LOCAL_ENABLED as _DETECT_LOCAL_ENABLED,
    TRACKING_FACE_DETECTOR_ENABLED as _FACE_DETECTOR_ENABLED,
)
from lelamp.service.sensing.crypto import CryptoSession, resolve_public_key
from lelamp.config import (
    YUNET_CONFIDENCE_THRESHOLD as _YUNET_CONF,
)

logger = logging.getLogger(__name__)

# --- Detection ---

# Local YOLOv8n (COCO) — ~300ms/frame on Allwinner A523 CPU. Used by default
# when target maps to a COCO class. Falls back to remote API for open-vocab.
# Weights are checked into the repo next to this file so deploy is one rsync
# and the Pi never needs internet at boot to start tracking. Source:
# https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt
_LOCAL_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "yolov8n.pt")
_LOCAL_IMGSZ = 320

# YuNet face detector (OpenCV built-in). Lighter than InsightFace, ~30ms/frame on
# Pi, no extra dependency. Used for target='face' so we don't fall back to the
# remote YOLOWorld (~1.3s) for what's a very common tracking target.
_YUNET_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models",
                                 "face_detection_yunet_2023mar.onnx")
# Aliases that route to the face detector instead of YOLO.
_FACE_TARGET_ALIASES = {"face", "human face", "khuôn mặt", "mặt"}

# Target label → COCO class index. Add aliases for natural Vietnamese/English usage.
_COCO_CLASSES = {
    # NOTE: "hand" / "face" intentionally NOT mapped — COCO has no hand/face class.
    # Mapping them to "person" caused bbox to lock onto whole body (25-78% frame),
    # triggering "object too close" stop. Let them fall through to YOLOWorld remote.
    "person": 0, "people": 0, "human": 0,
    "bicycle": 1, "car": 2, "motorcycle": 3, "airplane": 4, "bus": 5,
    "train": 6, "truck": 7, "boat": 8, "traffic light": 9, "fire hydrant": 10,
    "stop sign": 11, "parking meter": 12, "bench": 13,
    "bird": 14, "cat": 15, "dog": 16, "horse": 17, "sheep": 18, "cow": 19,
    "elephant": 20, "bear": 21, "zebra": 22, "giraffe": 23,
    "backpack": 24, "umbrella": 25, "handbag": 26, "tie": 27, "suitcase": 28,
    "frisbee": 29, "skis": 30, "snowboard": 31, "sports ball": 32, "ball": 32,
    "kite": 33, "baseball bat": 34, "baseball glove": 35, "skateboard": 36,
    "surfboard": 37, "tennis racket": 38, "bottle": 39, "wine glass": 40,
    "cup": 41, "fork": 42, "knife": 43, "spoon": 44, "bowl": 45,
    "banana": 46, "apple": 47, "sandwich": 48, "orange": 49, "broccoli": 50,
    "carrot": 51, "hot dog": 52, "pizza": 53, "donut": 54, "cake": 55,
    "chair": 56, "couch": 57, "potted plant": 58, "bed": 59, "dining table": 60,
    "toilet": 61, "tv": 62, "laptop": 63, "mouse": 64, "remote": 65,
    "keyboard": 66, "cell phone": 67, "phone": 67, "microwave": 68, "oven": 69,
    "toaster": 70, "sink": 71, "refrigerator": 72, "book": 73, "clock": 74,
    "vase": 75, "scissors": 76, "teddy bear": 77, "hair drier": 78, "toothbrush": 79,
}
# Remote API fallback (open vocabulary).
_DETECT_MODEL = "yoloworld"
_YOLO_ENDPOINT = f"/detect/{_DETECT_MODEL}"
_YOLO_TIMEOUT = 10.0

# Singleton local YOLO model — loaded lazily on first detection.
_local_yolo = None
_local_yolo_lock = threading.Lock()

# Singleton YuNet face detector — same lazy pattern.
_yunet = None
_yunet_lock = threading.Lock()


def _get_local_yolo():
    """Lazy-load YOLOv8n model from the repo path. Thread-safe singleton."""
    global _local_yolo
    if _local_yolo is not None:
        return _local_yolo
    with _local_yolo_lock:
        if _local_yolo is not None:
            return _local_yolo
        if not os.path.exists(_LOCAL_MODEL_PATH):
            logger.error(
                "YOLO weights missing at %s — re-deploy from repo (file is checked in). "
                "Falling back to remote YOLOWorld until present.",
                _LOCAL_MODEL_PATH,
            )
            return None
        try:
            from ultralytics import YOLO
            logger.info("Loading local YOLO model from %s", _LOCAL_MODEL_PATH)
            t0 = time.perf_counter()
            _local_yolo = YOLO(_LOCAL_MODEL_PATH)
            # Warm-up inference to trigger model compile/cache.
            import numpy as _np
            _local_yolo(_np.zeros((480, 640, 3), dtype=_np.uint8),
                        verbose=False, imgsz=_LOCAL_IMGSZ)
            logger.info("Local YOLO loaded + warmed up in %.0fms", (time.perf_counter() - t0) * 1000)
        except Exception as e:
            logger.error("Local YOLO load failed: %s", e)
            _local_yolo = None
    return _local_yolo


def _get_yunet():
    """Lazy-load YuNet face detector. Thread-safe singleton.

    Input size is set per-call via setInputSize before detect(), so we can keep
    one shared detector across frames of different sizes.
    """
    global _yunet
    if _yunet is not None:
        return _yunet
    with _yunet_lock:
        if _yunet is not None:
            return _yunet
        if not os.path.exists(_YUNET_MODEL_PATH):
            logger.error("YuNet weights missing at %s — face detection disabled",
                         _YUNET_MODEL_PATH)
            return None
        try:
            t0 = time.perf_counter()
            _yunet = cv2.FaceDetectorYN.create(
                _YUNET_MODEL_PATH,
                "",
                (320, 320),
                score_threshold=_YUNET_CONF,
                nms_threshold=0.3,
                top_k=50,
            )
            logger.info("YuNet face detector loaded in %.0fms (conf>=%.2f)",
                        (time.perf_counter() - t0) * 1000, _YUNET_CONF)
        except Exception as e:
            logger.error("YuNet load failed: %s", e)
            _yunet = None
    return _yunet


def _detect_face_yunet(frame: npt.NDArray[np.uint8]) -> Optional[Tuple[int, int, int, int]]:
    """Run YuNet on the frame, return the largest face bbox (x,y,w,h) or None.

    Largest-face policy: most prominent / closest face — predictable for a single
    tracking session. If multiple people, the closest one wins.
    """
    detector = _get_yunet()
    if detector is None:
        return None
    h, w = frame.shape[:2]
    try:
        detector.setInputSize((w, h))
        t0 = time.perf_counter()
        _, faces = detector.detect(frame)
        latency_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        logger.warning("YuNet detect failed: %s", e)
        return None
    if faces is None or len(faces) == 0:
        logger.info("[tracking_yunet] not found latency=%.0fms", latency_ms)
        return None
    # faces rows: [x, y, w, h, lm_x1..lm_y5, score]. Pick the largest by area.
    best = max(faces, key=lambda f: float(f[2]) * float(f[3]))
    x, y, fw, fh = int(best[0]), int(best[1]), int(best[2]), int(best[3])
    score = float(best[-1])
    # Clamp to frame in case the detector returns slightly negative coords.
    x = max(0, x); y = max(0, y)
    fw = max(1, min(fw, w - x)); fh = max(1, min(fh, h - y))
    logger.info("[tracking_yunet] face bbox=(%d,%d,%d,%d) score=%.3f count=%d latency=%.0fms",
                x, y, fw, fh, score, len(faces), latency_ms)
    return (x, y, fw, fh)

# --- Tuning knobs ---

# Fast loop target FPS — CSRT on Pi runs ~15-25ms/frame. Lowered from 15→10:
# Feetech STS3215 makes an audible click on each send_action (motor accel/decel
# spike). At 15fps × 4 substeps that's ~60 writes/sec = audible 60 Hz buzz.
# 10fps × 2 substeps ≈ 20 writes/sec → softer continuous motion.
FAST_LOOP_FPS = 10

# Hardware velocity limit for tracking (Feetech STS3215 Goal_Velocity register).
# 0 = unlimited (default). Lower = slower, smoother camera pan → ViT stays locked.
# Unit: steps/s. ~150 ≈ moderate tracking speed. Set to 0 to disable.
TRACKING_GOAL_VELOCITY = 150

# Hardware acceleration for tracking (Feetech STS3215 Acceleration register).
# 254 = max (default, snappy). Lower = gentler ramp up/down → less jerk.
# Range: 0-254. ~30 gives smooth glide without being too sluggish.
TRACKING_ACCELERATION = 30

# Camera field-of-view in degrees (horizontal). Used to convert px offset → degrees.
CAMERA_FOV_DEG = 60.0

# Gimbal gain: fraction of offset to correct each step (0-1).
# Lowered 0.9→0.6: less aggressive correction reduces camera shake on large
# offsets, giving ViT time to re-lock between servo fires.
GIMBAL_GAIN = 0.6

# Maximum servo step per fire (degrees).
# Lowered 5→3°: smaller steps = less camera pan per fire = ViT stays locked.
GIMBAL_MAX_STEP = 3.0

# Adaptive step: disabled amplification on large offsets (was ×2.0).
# Large offsets caused 10°/fire camera pan → ViT lost lock in <2s.
# Keeping multiplier at 1.0 so large offsets get the same small steps.
ADAPTIVE_GAIN_PX = 60
ADAPTIVE_GAIN_MULT = 1.0

# Per-axis dead zones as fraction of frame.
# Yaw larger (5%) — horizontal jitter is common, small dx not worth a motor move.
# Pitch smaller (3%) — vertical needs finer response for elbow tracking.
DEAD_ZONE_YAW_PCT   = 0.07
DEAD_ZONE_PITCH_PCT = 0.05

# EMA smoothing on pixel offset before servo command (0-1).
# Lower = smoother (less jitter) but slower response.
EMA_ALPHA = 0.5

# Settle delay (seconds) after each servo command.
# Doubled 0.025→0.05: more settle time = camera stabilises before ViT grabs
# next frame, reducing motion blur that causes bbox drift.
SERVO_SETTLE_S = 0.05

# YOLO background re-detect interval (seconds).
# Local YOLOv8n runs ~300-700ms/call on Allwinner A523. At 500ms interval
# it saturated all CPU cores → camera MJPEG stream stalled.
# 1.5s gives the CPU breathing room while still catching tracker drift.
YOLO_REDETECT_S = 1.5

# How many consecutive tracker-update miss frames before retrying.
# Raised: ViT honestly returns ok=False on transient low-confidence frames.
YOLO_MAX_MISS = 30

# Motion detection: EMA-offset delta between consecutive frames to count as "moving".
# Tuned for ~10fps CSRT (100ms/frame) — stationary CSRT jitter ≈ 5-15px EMA delta.
MOTION_THRESHOLD_PX = 20

# Consecutive stable frames needed to declare object "settled".
# At 10fps: 2 frames ≈ 200ms of stillness before servo fires.
MOTION_SETTLE_FRAMES = 2

# Cooldown after servo fire (seconds) — ignore motion detection while camera
# stabilises after a move. Prevents servo shake → fake MOVE → immediate re-fire loop.
SERVO_COOLDOWN_S = 0.10

SERVO_SUBSTEP_DEG   = 1.5   # bigger per-substep: fewer total writes → fewer audible clicks
# Spaced wider so the motor has time between commands to glide smoothly to
# each intermediate point, instead of getting retargeted before it settles
# (which produced the click train).
SERVO_SUBSTEP_SLEEP = 0.030
# Minimum substeps per fire. Lowered from 4→2: ramping over 4 writes turned
# the motor into a high-frequency clicker. 2 writes per fire is enough to
# avoid the worst burst-then-idle gap without the audible buzz.
SERVO_MIN_SUBSTEPS  = 2

# Pitch distribution across 3 joints.
# Empirical: only wrist_pitch is pure rotation. base+elbow primarily translate
# camera (kinematic coupling) → object grows in frame but doesn't move toward
# center. Use wrist alone for predictable pitch control.
PITCH_WEIGHT_BASE  = 0.10
PITCH_WEIGHT_ELBOW = 0.90
PITCH_WEIGHT_WRIST = 0.0

# Edge proximity boost — when object nears frame edge, multiply correction
# to pull it back toward center before it exits the frame.
EDGE_BOOST_THRESHOLD = 0.30   # fraction of frame (30%)
EDGE_BOOST_MULT      = 1.5

# Maximum tracking duration (seconds) — auto-stop to save motor/CPU.
MAX_TRACK_DURATION_S = 300  # 5 minutes

# Servo position limits (degrees).
YAW_MIN, YAW_MAX = -135.0, 135.0
BASE_PITCH_MIN, BASE_PITCH_MAX = -90.0, 30.0
ELBOW_PITCH_MIN, ELBOW_PITCH_MAX = -90.0, 90.0
WRIST_PITCH_MIN, WRIST_PITCH_MAX = -90.0, 90.0

# YOLOWorld detection quality filters.
DETECT_MIN_AREA_RATIO = 0.003
DETECT_MAX_AREA_RATIO = 0.80
DETECT_MIN_CONFIDENCE = 0.15  # lowered to catch phone at angles/back-facing

# Ghost-lock detection via tracker confidence (ViT only).
CONFIDENCE_THRESHOLD = 0.15
MAX_LOW_CONFIDENCE_FRAMES = 10
# When the detector (YuNet / YOLO) hasn't confirmed for TRUST_TRACKER_S, fall
# back to ViT's own confidence: if it's above this threshold, keep firing PID
# (the tracker still has a solid lock — common when face moves fast and YuNet
# misses a few frames). Below this → freeze servo, wait for detector.
TRACKER_TRUST_CONF = 0.4

# PID gains for servo control (industry pattern: PyImageSearch face tracking).
# KP lowered (0.04→0.025 yaw, 0.05→0.03 pitch): smaller per-fire step, gentler
# chase. KD lowered too — D term amplifies CSRT bbox jitter into servo jerks.
PID_YAW_KP, PID_YAW_KI, PID_YAW_KD = 0.025, 0.002, 0.002
PID_PITCH_KP, PID_PITCH_KI, PID_PITCH_KD = 0.03, 0.002, 0.0025
PID_OUTPUT_MAX_DEG = 5.0
PID_INTEGRAL_MAX = 30.0


class PID:
    """Time-aware PID with anti-windup. Industry-standard servo control."""

    def __init__(self, kp: float, ki: float, kd: float,
                 output_min: float = -PID_OUTPUT_MAX_DEG,
                 output_max: float = PID_OUTPUT_MAX_DEG,
                 integral_min: float = -PID_INTEGRAL_MAX,
                 integral_max: float = PID_INTEGRAL_MAX):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.output_min, self.output_max = output_min, output_max
        self.integral_min, self.integral_max = integral_min, integral_max
        self._prev_error: float = 0.0
        self._integral: float = 0.0
        self._last_t: Optional[float] = None

    def reset(self) -> None:
        self._prev_error = 0.0
        self._integral = 0.0
        self._last_t = None

    def update(self, error: float) -> float:
        now = time.perf_counter()
        dt = 0.0 if self._last_t is None else (now - self._last_t)
        self._last_t = now
        p = self.kp * error
        if dt > 0:
            self._integral += error * dt
            self._integral = max(self.integral_min, min(self.integral_max, self._integral))
        i = self.ki * self._integral
        d = self.kd * (error - self._prev_error) / dt if dt > 0 else 0.0
        self._prev_error = error
        return max(self.output_min, min(self.output_max, p + i + d))


@dataclass
class TrackingState:
    """Mutable state for the active tracking session."""
    target_label: str = ""
    tracker: Optional[cv2.Tracker] = None
    bbox: Optional[Tuple[int, int, int, int]] = None
    confidence: Optional[float] = None
    low_confidence_frames: int = 0
    running: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


class TrackerService:
    """Manages a single object-tracking session with gimbal-style servo follow."""

    def __init__(self):
        self._state = TrackingState()
        self._lock = threading.Lock()
        # Serializes start() so two near-simultaneous /servo/track requests don't
        # both enter detect_object (5-7s) and end up spawning two tracking
        # threads that fight over the same servo state.
        self._start_lock = threading.Lock()
        self.last_error: str = ""
        self._yaw_pid = PID(PID_YAW_KP, PID_YAW_KI, PID_YAW_KD)
        self._pitch_pid = PID(PID_PITCH_KP, PID_PITCH_KI, PID_PITCH_KD)

        self._crypto: CryptoSession | None = None
        if config.DL_ENCRYPTION_ENABLED:
            public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
            if public_key is not None:
                self._crypto = CryptoSession(public_key)
                logger.info("Tracker: encryption enabled for remote YOLOWorld")
            elif config.DL_ENCRYPTION_REQUIRED:
                logger.error("Tracker: encryption required but no public key available")

    @property
    def is_tracking(self) -> bool:
        return self._state.running.is_set()

    @property
    def status(self) -> dict:
        s = self._state
        return {
            "tracking": s.running.is_set(),
            "target": s.target_label or None,
            "bbox": list(s.bbox) if s.bbox else None,
            "confidence": s.confidence,
        }

    def detect_object(self, frame: npt.NDArray[np.uint8], target: str) -> Optional[Tuple[int, int, int, int]]:
        """Detect an object by name. Tries local YOLOv8n first (fast, COCO classes),
        falls back to remote YOLOWorld API for open-vocab targets.

        Returns (x, y, w, h) top-left bbox or None if not found.
        """
        target_key = (target or "").lower().strip()

        # --- Path 0: YuNet face detector (target = face) ---
        # COCO has no face class; this avoids the ~1.3s remote round-trip for what
        # is a common tracking target.
        if _FACE_DETECTOR_ENABLED and target_key in _FACE_TARGET_ALIASES:
            face_bbox = _detect_face_yunet(frame)
            if face_bbox is not None:
                return face_bbox
            # YuNet missed — fall through to remote YOLOWorld below.

        # --- Path 1: local YOLOv8n (if target maps to COCO class) ---
        coco_idx = _COCO_CLASSES.get(target_key)
        if _DETECT_LOCAL_ENABLED and coco_idx is not None:
            model = _get_local_yolo()
            if model is not None:
                t_req = time.perf_counter()
                try:
                    results = model(frame, verbose=False, imgsz=_LOCAL_IMGSZ,
                                    classes=[coco_idx], conf=DETECT_MIN_CONFIDENCE)
                    t_ms = (time.perf_counter() - t_req) * 1000
                    h_fr, w_fr = frame.shape[:2]
                    frame_area = float(h_fr * w_fr)
                    best = None
                    for r in results:
                        if r.boxes is None or len(r.boxes) == 0:
                            continue
                        for b in r.boxes:
                            x1, y1, x2, y2 = b.xyxy[0].tolist()
                            conf = float(b.conf[0])
                            bw = int(x2 - x1)
                            bh = int(y2 - y1)
                            area_ratio = (bw * bh) / frame_area if frame_area > 0 else 0.0
                            if not (DETECT_MIN_AREA_RATIO <= area_ratio <= DETECT_MAX_AREA_RATIO):
                                continue
                            if best is None or conf > best[1]:
                                best = ((int(x1), int(y1), bw, bh), conf, area_ratio)
                    if best is not None:
                        bbox, conf, area_ratio = best
                        logger.info("[tracking_yolo_local] target='%s' bbox=%s conf=%.3f area=%.1f%% latency=%.0fms",
                                    target, bbox, conf, area_ratio * 100, t_ms)
                        return bbox
                    logger.info("[tracking_yolo_local] target='%s' not found latency=%.0fms", target, t_ms)
                    return None
                except Exception as e:
                    logger.warning("Local YOLO inference failed: %s — falling back to remote", e)
        elif coco_idx is None:
            logger.info("[tracking_yolo] target='%s' not in COCO — using remote", target)

        # --- Path 2: remote YOLOWorld API (open-vocab fallback) ---
        from lelamp.config import DL_BACKEND_URL, DL_API_KEY
        if not DL_BACKEND_URL:
            logger.error("YOLOWorld: DL_BACKEND_URL not configured")
            return None

        url = DL_BACKEND_URL.rstrip("/") + "/" + _YOLO_ENDPOINT.strip("/")
        logger.info("[tracking_yolo_request] target='%s' url=%s", target, url)
        t_req = time.perf_counter()
        try:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img_b64 = base64.b64encode(buf.tobytes()).decode()

            payload = {"image_b64": img_b64, "classes": [target]}
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if DL_API_KEY:
                headers["X-API-Key"] = DL_API_KEY
            if self._crypto is not None:

                resp = requests.post(
                    url,
                    data=self._crypto.wrap_http_request(json.dumps(payload).encode()),
                    headers=headers,
                    timeout=_YOLO_TIMEOUT,
                )
            else:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=_YOLO_TIMEOUT,
                )
            if resp.status_code != 200:
                logger.warning("YOLOWorld HTTP %d: %s", resp.status_code, resp.text[:200])
                return None

            if self._crypto is not None:

                detections = json.loads(self._crypto.unwrap_http_response(resp.content))
            else:
                detections = resp.json()
            if not detections:
                logger.info("YOLOWorld: '%s' not found in frame", target)
                return None

            frame_area = float(frame.shape[0] * frame.shape[1])
            valid = []
            for d in detections:
                cx, cy, w, h = d["xywh"]
                conf = d.get("confidence", 0)
                area_ratio = (w * h) / frame_area if frame_area > 0 else 0.0
                cname = d.get("class_name", "?")
                if conf < DETECT_MIN_CONFIDENCE:
                    reason = "REJECTED (conf)"
                elif not (DETECT_MIN_AREA_RATIO <= area_ratio <= DETECT_MAX_AREA_RATIO):
                    reason = "REJECTED (size)"
                else:
                    reason = "ACCEPTED"
                logger.info(
                    "  YOLO candidate: class='%s' conf=%.3f bbox=(%d,%d,%d,%d) area=%.1f%% %s",
                    cname, conf, int(cx - w / 2), int(cy - h / 2), int(w), int(h),
                    area_ratio * 100, reason,
                )
                if reason == "ACCEPTED":
                    valid.append(d)

            if not valid:
                logger.warning(
                    "YOLOWorld: '%s' — %d detection(s) but none passed filters "
                    "(conf >= %.2f, area %.1f%%–%.1f%%)",
                    target, len(detections), DETECT_MIN_CONFIDENCE,
                    DETECT_MIN_AREA_RATIO * 100, DETECT_MAX_AREA_RATIO * 100,
                )
                return None

            best = max(valid, key=lambda d: d.get("confidence", 0))
            cx, cy, w, h = best["xywh"]
            x = int(cx - w / 2)
            y = int(cy - h / 2)
            bbox = (x, y, int(w), int(h))
            latency_ms = (time.perf_counter() - t_req) * 1000
            self._state.confidence = round(best["confidence"], 3)
            logger.info("YOLOWorld: '%s' found at bbox=%s conf=%.3f", target, bbox, best["confidence"])
            logger.info("[tracking_yolo_response] target='%s' found=True bbox=%s conf=%.3f latency=%.0fms",
                        target, bbox, best["confidence"], latency_ms)
            return bbox
        except Exception as e:
            logger.error("YOLOWorld detect failed: %s", e)
            return None

    def start(
        self,
        bbox: Optional[Tuple[int, int, int, int]] = None,
        target_label="",
        camera_capture=None,
        animation_service=None,
    ) -> bool:
        """Start tracking an object.

        If bbox is provided, use it directly. Otherwise, auto-detect via YOLOWorld.
        target_label accepts str or list[str] — first non-empty label is used.
        """
        if camera_capture is None or animation_service is None:
            self.last_error = "camera or animation service not available"
            logger.error("tracker start: %s", self.last_error)
            return False

        if isinstance(target_label, (list, tuple)):
            target_label = next((t for t in target_label if t), "")

        # Serialize concurrent /servo/track calls. detect_object can take 5-7s
        # (remote YOLOWorld or first-time local YOLO load). Without this lock,
        # two near-simultaneous calls both pass self.stop() (nothing to stop yet)
        # and spawn two tracking threads that race over servo state.
        if not self._start_lock.acquire(blocking=False):
            self.last_error = "another tracking session is initializing — ignoring duplicate request"
            logger.warning("tracker start: %s target='%s'", self.last_error, target_label)
            return False
        try:
            self.stop()
            return self._start_locked(bbox, target_label, camera_capture, animation_service)
        finally:
            self._start_lock.release()

    def _start_locked(
        self,
        bbox: Optional[Tuple[int, int, int, int]],
        target_label: str,
        camera_capture,
        animation_service,
    ) -> bool:
        """Body of start() — runs while _start_lock is held."""

        # Freeze servos so YOLO + tracker init see a sharp, stable frame.
        settle_s = 0.30
        t_req = time.perf_counter()
        animation_service.freeze()
        try:
            time.sleep(settle_s)
            t_after_settle = time.perf_counter()

            frame = camera_capture.last_frame
            if frame is None:
                self.last_error = "no frame available from camera"
                logger.error("tracker start: %s", self.last_error)
                animation_service.unfreeze()
                return False
            frame = frame.copy()

            t_yolo_ms = 0.0
            if bbox is None:
                if not target_label:
                    self.last_error = "need either bbox or target label"
                    logger.error("tracker start: %s", self.last_error)
                    animation_service.unfreeze()
                    return False
                t_yolo0 = time.perf_counter()
                bbox = self.detect_object(frame, target_label)
                t_yolo_ms = (time.perf_counter() - t_yolo0) * 1000
                if bbox is None:
                    self.last_error = f"'{target_label}' not found in frame"
                    logger.info("[track-start] settle=%.0fms yolo=%.0fms result=missed target='%s'",
                                (t_after_settle - t_req) * 1000, t_yolo_ms, target_label)
                    animation_service.unfreeze()
                    return False
        except Exception:
            animation_service.unfreeze()
            raise

        tracker = self._create_tracker()
        if tracker is None:
            logger.error("No OpenCV tracker available")
            animation_service.unfreeze()
            return False

        t_init0 = time.perf_counter()
        try:
            ok = tracker.init(frame, bbox)
        except Exception as e:
            logger.error("tracker init exception for bbox %s: %s", bbox, e)
            animation_service.unfreeze()
            return False
        if ok is False:
            logger.error("tracker init failed for bbox %s", bbox)
            animation_service.unfreeze()
            return False
        t_init_ms = (time.perf_counter() - t_init0) * 1000
        t_total_ms = (time.perf_counter() - t_req) * 1000
        logger.info(
            "[track-start] settle=%.0fms yolo=%.0fms init=%.0fms total=%.0fms bbox=%s target='%s'",
            (t_after_settle - t_req) * 1000, t_yolo_ms, t_init_ms, t_total_ms, bbox, target_label,
        )

        with self._lock:
            self._state = TrackingState(
                target_label=target_label,
                tracker=tracker,
                bbox=bbox,
            )
            self._state.running.set()
            self._state.thread = threading.Thread(
                target=self._track_loop,
                args=(camera_capture, animation_service),
                daemon=True,
                name="servo-tracker",
            )
            self._state.thread.start()

        animation_service.unfreeze()
        animation_service.dispatch("play", "tracking")
        logger.info("Tracking started: '%s' bbox=%s — playing tracking animation", target_label, bbox)
        return True

    def stop(self):
        """Stop the current tracking session."""
        with self._lock:
            if not self._state.running.is_set():
                return
            self._state.running.clear()
            t = self._state.thread

        if t and t.is_alive():
            # Tracking loop iterations can take up to ~250ms (CSRT + 5 sub-step
            # ramp + frame settle). 10s gives ~40 iterations of headroom so we
            # never return while the old thread is still racing the servo with a
            # new session's commands.
            t.join(timeout=10.0)
            if t.is_alive():
                logger.error("[tracker.stop] previous tracking thread refused to exit after 10s")

        logger.info("Tracking stopped: '%s'", self._state.target_label)

    def _fire_gimbal(self, dx: float, dy: float, frame_width: int, frame_height: int, animation_service) -> float:
        """Send one proportional gimbal correction toward the target offset.

        Args:
            dx: horizontal pixel offset from frame center (+ = right).
            dy: vertical pixel offset from frame center (+ = below center).
            frame_width: frame width in pixels.
            frame_height: frame height in pixels (used for edge boost).
            animation_service: provides bus_lock and robot.send_action().

        Returns:
            Servo command round-trip time in milliseconds.
        """
        target = self._compute_gimbal_target(dx, dy, frame_width, frame_height)
        logger.info(
            "[servo-pending] yaw=%.1f→%.1f pitch=%.1f→%.1f elbow=%.1f→%.1f offset=(%.0f,%.0f)",
            self._track_yaw, target["base_yaw.pos"],
            self._track_base_pitch, target["base_pitch.pos"],
            self._track_elbow_pitch, target["elbow_pitch.pos"],
            dx, dy,
        )
        return self._send_gimbal_target(target, animation_service)

    def _compute_gimbal_target(self, dx: float, dy: float, frame_width: int, _frame_height: int = 480) -> dict:
        """Compute target servo positions from pixel offset — no API call."""
        offset_mag = (dx ** 2 + dy ** 2) ** 0.5
        step_cap = GIMBAL_MAX_STEP * (ADAPTIVE_GAIN_MULT if offset_mag > ADAPTIVE_GAIN_PX else 1.0)
        deg_per_px = CAMERA_FOV_DEG / frame_width

        yaw_step    = max(-step_cap, min(step_cap, GIMBAL_GAIN * dx * deg_per_px))
        pitch_total = max(-step_cap, min(step_cap, GIMBAL_GAIN * dy * deg_per_px))
        return {
            "base_yaw.pos":    max(YAW_MIN,         min(YAW_MAX,         self._track_yaw         + yaw_step)),
            "base_pitch.pos":  max(BASE_PITCH_MIN,  min(BASE_PITCH_MAX,  self._track_base_pitch  + pitch_total * PITCH_WEIGHT_BASE)),
            "elbow_pitch.pos": max(ELBOW_PITCH_MIN, min(ELBOW_PITCH_MAX, self._track_elbow_pitch + pitch_total * PITCH_WEIGHT_ELBOW)),
            "wrist_pitch.pos": max(WRIST_PITCH_MIN, min(WRIST_PITCH_MAX, self._track_wrist_pitch - pitch_total * PITCH_WEIGHT_WRIST)),
        }

    def _send_gimbal_target(self, target: dict, animation_service) -> float:
        """Send servo to target via smooth sub-steps ≤ SERVO_SUBSTEP_DEG each.

        All 4 joints move together per step — avoids partial-command issues.
        move_to() was tried but its 200ms ramp caused CSRT drift during camera
        motion, leading to erratic corrections. Sub-step (~40-80ms) is fast
        enough that the tracker stays stable.
        Returns total command time in ms.
        """
        start = {
            "base_yaw.pos":    self._track_yaw,
            "base_pitch.pos":  self._track_base_pitch,
            "elbow_pitch.pos": self._track_elbow_pitch,
            "wrist_pitch.pos": self._track_wrist_pitch,
        }
        deltas = {k: target[k] - start[k] for k in start}
        max_delta = max(abs(v) for v in deltas.values())
        # Force a minimum number of substeps even for tiny moves so the motor
        # ramps over the whole loop interval instead of bursting in 2ms then
        # sitting idle 60ms. This is what produces the "continuous joint glide"
        # feel rather than discrete chunks.
        n_steps = max(SERVO_MIN_SUBSTEPS, math.ceil(max_delta / SERVO_SUBSTEP_DEG))

        t0 = time.perf_counter()
        for i in range(1, n_steps + 1):
            alpha = i / n_steps
            step = {k: start[k] + deltas[k] * alpha for k in start}
            with animation_service.bus_lock:
                animation_service.robot.send_action(step)
            if i < n_steps:
                time.sleep(SERVO_SUBSTEP_SLEEP)
        t_ms = (time.perf_counter() - t0) * 1000

        logger.info(
            "[servo-actual] FIRE yaw=%.1f→%.1f pitch=%.1f→%.1f elbow=%.1f→%.1f wrist=%.1f→%.1f steps=%d cmd=%.0fms",
            start["base_yaw.pos"],    target["base_yaw.pos"],
            start["base_pitch.pos"],  target["base_pitch.pos"],
            start["elbow_pitch.pos"], target["elbow_pitch.pos"],
            start["wrist_pitch.pos"], target["wrist_pitch.pos"],
            n_steps, t_ms,
        )
        self._track_yaw         = target["base_yaw.pos"]
        self._track_base_pitch  = target["base_pitch.pos"]
        self._track_elbow_pitch = target["elbow_pitch.pos"]
        self._track_wrist_pitch = target["wrist_pitch.pos"]
        time.sleep(SERVO_SETTLE_S)
        return t_ms

    _VIT_MODEL = os.path.join(os.path.dirname(__file__), "models", "vittrack.onnx")

    @staticmethod
    def _create_tracker():
        """Create best available OpenCV tracker. CSRT/KCF removed in cv2 4.10+.
        Prefer TrackerVit (ViT-based, accurate) → MIL fallback."""
        def _make_vit():
            params = cv2.TrackerVit_Params()
            params.net = TrackerService._VIT_MODEL
            return cv2.TrackerVit.create(params)

        # ViT first — has getTrackingScore() for ghost-lock detection.
        candidates = [
            ("ViT",  _make_vit),
            ("CSRT", lambda: cv2.TrackerCSRT.create()),
            ("KCF",  lambda: cv2.TrackerKCF.create()),
            ("MIL",  lambda: cv2.TrackerMIL.create()),
        ]
        for name, factory in candidates:
            try:
                tracker = factory()
                has_score = hasattr(tracker, "getTrackingScore")
                logger.info("Using OpenCV tracker: %s (confidence=%s)", name, "yes" if has_score else "no")
                return tracker
            except (AttributeError, cv2.error, Exception):
                continue
        return None

    def _get_confidence(self) -> float:
        """ViT confidence score; other trackers return 1.0 (no signal)."""
        try:
            return float(self._state.tracker.getTrackingScore())
        except (AttributeError, Exception):
            return 1.0

    def _fire_pid(self, yaw_step: float, pitch_correction: float, animation_service) -> float:
        """Apply PID outputs. yaw → base_yaw. pitch → distributed across base/elbow/wrist.

        Pitch sign — empirical evidence over time:
          2026-05-13: claimed base+ = UP, elbow+ = DOWN, wrist+ = UP, code used
                      `wrist - pitch_correction` to look UP when dy<0.
          2026-05-14: log shows face dy=-180 → pid pitch=-5 → code wrote
                      wrist -67→-7 (INCREASE), and the lamp visibly tilted DOWN.
                      So wrist+ is actually DOWN at the poses we encounter, and
                      the sign was inverted. Flipped to `wrist + pitch_correction`
                      so the camera now moves toward dy (per the long-standing
                      memory rule pitch_deg = dy*k applied as wrist_new = wrist + pitch_deg).
        """
        target = {
            "base_yaw.pos":    max(YAW_MIN,         min(YAW_MAX,         self._track_yaw         + yaw_step)),
            "base_pitch.pos":  max(BASE_PITCH_MIN,  min(BASE_PITCH_MAX,  self._track_base_pitch  + pitch_correction * PITCH_WEIGHT_BASE)),
            "elbow_pitch.pos": max(ELBOW_PITCH_MIN, min(ELBOW_PITCH_MAX, self._track_elbow_pitch + pitch_correction * PITCH_WEIGHT_ELBOW)),
            "wrist_pitch.pos": max(WRIST_PITCH_MIN, min(WRIST_PITCH_MAX, self._track_wrist_pitch + pitch_correction * PITCH_WEIGHT_WRIST)),
        }
        # Warn loudly when an axis has saturated against its mechanical limit and
        # the PID is still demanding more travel in that direction — camera
        # physically can't follow further; only re-centering the lamp helps.
        if abs(yaw_step) >= 0.1 and (
            (yaw_step < 0 and self._track_yaw <= YAW_MIN + 0.5) or
            (yaw_step > 0 and self._track_yaw >= YAW_MAX - 0.5)
        ):
            logger.warning("[saturation] yaw at limit %.1f° but PID still demanding %.2f° — recenter lamp",
                           self._track_yaw, yaw_step)
        if abs(pitch_correction) >= 0.1 and PITCH_WEIGHT_WRIST > 0 and (
            (pitch_correction > 0 and self._track_wrist_pitch >= WRIST_PITCH_MAX - 0.5) or
            (pitch_correction < 0 and self._track_wrist_pitch <= WRIST_PITCH_MIN + 0.5)
        ):
            logger.warning("[saturation] wrist at limit %.1f° but PID still demanding pitch=%.2f° — recenter lamp",
                           self._track_wrist_pitch, pitch_correction)
        return self._send_gimbal_target(target, animation_service)

    # --- Internal tracking loop ---

    def _track_loop(self, camera_capture, animation_service):
        """Background loop: CSRT at FAST_LOOP_FPS + YOLO background correction."""
        state = self._state

        animation_service._hold_mode = True
        animation_service._tracking_active = True
        logger.info("Servo hold mode + tracking lock ON")

        _tracking_motors = ["base_yaw", "base_pitch", "elbow_pitch", "wrist_pitch"]
        try:
            with animation_service.bus_lock:
                if TRACKING_GOAL_VELOCITY > 0:
                    animation_service.robot.bus.sync_write(
                        "Goal_Velocity", {m: TRACKING_GOAL_VELOCITY for m in _tracking_motors}
                    )
                animation_service.robot.bus.sync_write(
                    "Acceleration", {m: TRACKING_ACCELERATION for m in _tracking_motors}
                )
            logger.info("[tracking] Goal_Velocity=%d Acceleration=%d", TRACKING_GOAL_VELOCITY, TRACKING_ACCELERATION)
        except Exception as e:
            logger.warning("[tracking] Failed to set motor params: %s", e)

        # Read initial servo positions — track internally after this.
        try:
            from lelamp.service.motors.animation_service import _motor_positions_from_bus
            with animation_service.bus_lock:
                init_pos = _motor_positions_from_bus(animation_service.robot)
            self._track_yaw = init_pos.get("base_yaw.pos", 0.0)
            self._track_base_pitch = init_pos.get("base_pitch.pos", 0.0)
            self._track_elbow_pitch = init_pos.get("elbow_pitch.pos", 0.0)
            self._track_wrist_pitch = init_pos.get("wrist_pitch.pos", 0.0)
        except Exception:
            self._track_yaw = 0.0
            self._track_base_pitch = 0.0
            self._track_elbow_pitch = 0.0
            self._track_wrist_pitch = 0.0

        # Reset PID state for a clean session.
        self._yaw_pid.reset()
        self._pitch_pid.reset()

        ema_dx: Optional[float] = None
        ema_dy: Optional[float] = None
        prev_dx: Optional[float] = None   # EMA offset from previous frame (motion detection)
        prev_dy: Optional[float] = None
        motion_state = "INIT"             # INIT → STILL or MOVING
        stable_count = 0                  # consecutive stable frames counter
        last_servo_t: float = 0.0         # timestamp of last servo fire (for cooldown)
        miss_count = 0
        yolo_miss_count = 0   # consecutive YOLO misses — ghost tracking detection
        retry_count = 0
        MAX_TRACKING_RETRIES = 4
        frame_count = 0
        t_csrt_acc = 0.0   # accumulated CSRT update time
        t_servo_acc = 0.0  # accumulated servo command time (only frames that fired)
        servo_count = 0    # frames where servo actually fired
        track_start_t = time.perf_counter()
        last_yolo_t = track_start_t
        # Detector-gated trust: skip servo if YOLO hasn't confirmed target recently.
        last_yolo_confirm_t = track_start_t
        TRUST_TRACKER_S = 2.5      # With redetect=1.5s, allow ~1 missed redetect before suspect
        STOP_NO_YOLO_S = 20.0
        fps_t0 = track_start_t

        # Queue for background YOLO results (maxsize=1 → latest result only).
        yolo_q: queue.Queue = queue.Queue(maxsize=1)
        yolo_running = threading.Event()

        def _do_retry() -> bool:
            """Play search animation, try YOLO, reinit tracker. Returns True to continue."""
            nonlocal retry_count, miss_count, yolo_miss_count, ema_dx, ema_dy
            nonlocal prev_dx, prev_dy, motion_state, stable_count, last_yolo_t
            retry_count += 1
            if retry_count > MAX_TRACKING_RETRIES:
                logger.warning("[retry] exhausted %d retries, stopping", MAX_TRACKING_RETRIES)
                return False
            logger.info("[retry] attempt %d/%d (soft)", retry_count, MAX_TRACKING_RETRIES)
            self._yaw_pid.reset()
            self._pitch_pid.reset()
            # Try YOLO detect on fresh frame
            _f = camera_capture.last_frame
            if _f is not None:
                _bbox = self.detect_object(_f, state.target_label)
                if _bbox is not None:
                    _t = self._create_tracker()
                    if _t is not None:
                        try:
                            if _t.init(_f, _bbox) is not False:
                                state.tracker = _t
                                state.bbox = _bbox
                                logger.info("[retry] tracker reinit OK bbox=%s", _bbox)
                        except Exception as _e:
                            logger.warning("[retry] tracker init failed: %s", _e)
            # Reset per-attempt state
            miss_count = 0
            yolo_miss_count = 0
            ema_dx = ema_dy = None
            prev_dx = prev_dy = None
            motion_state = "INIT"
            stable_count = 0
            last_yolo_t = 0  # force YOLO on next frame
            while True:  # drain stale YOLO queue
                try: yolo_q.get_nowait()
                except queue.Empty: break
            return True

        def _fire_yolo(frame_snap: npt.NDArray[np.uint8]) -> None:
            t0_yolo = time.perf_counter()
            result = self.detect_object(frame_snap, state.target_label)
            t_yolo_ms = (time.perf_counter() - t0_yolo) * 1000
            logger.info("[yolo-bg] detect=%.0fms result=%s bbox=%s target='%s'",
                        t_yolo_ms, "found" if result is not None else "missed", result, state.target_label)
            if result is None:
                logger.info("[tracking_yolo_response] target='%s' found=False latency=%.0fms", state.target_label, t_yolo_ms)
            try:
                yolo_q.put_nowait(result)
            except queue.Full:
                pass
            finally:
                yolo_running.clear()

        try:
            while state.running.is_set():
                t0 = time.perf_counter()

                frame = camera_capture.last_frame
                if frame is None:
                    time.sleep(1.0 / FAST_LOOP_FPS)
                    continue

                h_fr, w_fr = frame.shape[:2]
                t_csrt0 = time.perf_counter()
                ok, new_bbox = state.tracker.update(frame)
                t_csrt_ms = (time.perf_counter() - t_csrt0) * 1000
                t_csrt_acc += t_csrt_ms

                # Confidence-based ghost-lock detection (ViT only).
                confidence = self._get_confidence()
                state.confidence = confidence
                if ok and confidence < CONFIDENCE_THRESHOLD:
                    state.low_confidence_frames += 1
                    logger.info("[conf] low %.3f (%d/%d) target='%s'",
                                confidence, state.low_confidence_frames,
                                MAX_LOW_CONFIDENCE_FRAMES, state.target_label)
                    if state.low_confidence_frames >= MAX_LOW_CONFIDENCE_FRAMES:
                        logger.warning("Tracker lost '%s' (conf=%.3f for %d frames) — stopping",
                                       state.target_label, confidence, state.low_confidence_frames)
                        break
                    time.sleep(1.0 / FAST_LOOP_FPS)
                    continue
                state.low_confidence_frames = 0

                if not ok:
                    miss_count += 1
                    logger.info("[search] CSRT miss %d/%d target='%s'", miss_count, YOLO_MAX_MISS, state.target_label)
                    if miss_count == 1:
                        # First miss: force YOLO immediately instead of waiting for interval
                        last_yolo_t = 0
                    # Sweep base_yaw to search for object — alternates direction every 8 frames
                    _sweep_dir = 1 if ((miss_count - 1) // 8) % 2 == 0 else -1
                    _new_yaw = max(YAW_MIN, min(YAW_MAX, self._track_yaw + 2.0 * _sweep_dir))
                    with animation_service.bus_lock:
                        animation_service.robot.send_action({"base_yaw.pos": _new_yaw})
                    self._track_yaw = _new_yaw
                    if miss_count >= YOLO_MAX_MISS:
                        if _do_retry():
                            continue
                        break
                    time.sleep(1.0 / FAST_LOOP_FPS)
                    continue

                miss_count = 0
                state.bbox = tuple(int(v) for v in new_bbox)
                bx, by, bw, bh = state.bbox

                frame_area = float(h_fr * w_fr)
                bbox_ratio = (bw * bh) / frame_area
                # "Object too close" stop removed intentionally — servo PID drives off
                # the centroid, not bbox size, so a person filling the frame can still
                # be tracked. Stopping just because they stood up close was killing
                # every session within 1–2s on the Pi. If they back away, bbox shrinks
                # naturally and tracking continues.

                # Ghost-lock: bbox shrunk to a sliver (typically locked on frame edge).
                if bbox_ratio < DETECT_MIN_AREA_RATIO:
                    logger.warning("[bbox] ghost-lock: %dx%d area=%.2f%% — stopping",
                                   bw, bh, bbox_ratio * 100)
                    break

                # Bbox drifted large — fire YOLO to correct, but DON'T skip the frame.
                # Skipping creates a dead spiral: tracker keeps bloating each iteration
                # while YOLO misses, until bbox crosses the stop threshold. Letting the
                # loop continue keeps the servo chasing while YOLO works in background.
                if bbox_ratio > DETECT_MAX_AREA_RATIO:
                    logger.warning("[bbox] large (%.1f%% > %.1f%%) — firing YOLO bg, keep tracking",
                                   bbox_ratio * 100, DETECT_MAX_AREA_RATIO * 100)
                    if not yolo_running.is_set() and state.target_label:
                        yolo_running.set()
                        snap = frame.copy()
                        threading.Thread(
                            target=_fire_yolo, args=(snap,), daemon=True, name="yolo-worker"
                        ).start()

                cx_obj = bx + bw / 2.0
                cy_obj = by + bh / 2.0

                # EMA smoothing on pixel offset (not on absolute position).
                raw_dx = cx_obj - w_fr / 2.0
                raw_dy = cy_obj - h_fr / 2.0
                if ema_dx is None or ema_dy is None:
                    ema_dx, ema_dy = raw_dx, raw_dy
                else:
                    ema_dx = EMA_ALPHA * raw_dx + (1.0 - EMA_ALPHA) * ema_dx
                    ema_dy = EMA_ALPHA * raw_dy + (1.0 - EMA_ALPHA) * ema_dy
                dx, dy = float(ema_dx), float(ema_dy)

                # --- tracking_object log: position, motion, direction ---
                offset_mag = (dx ** 2 + dy ** 2) ** 0.5
                screen_x_pct = (cx_obj / w_fr) * 100
                screen_y_pct = (cy_obj / h_fr) * 100
                quadrant = ("TOP" if dy < 0 else "BOT") + "_" + ("LEFT" if dx < 0 else "RIGHT")
                if prev_dx is not None and prev_dy is not None:
                    ddx, ddy = dx - prev_dx, dy - prev_dy
                    if (ddx ** 2 + ddy ** 2) ** 0.5 > 2:
                        angle = ["→", "↗", "↑", "↖", "←", "↙", "↓", "↘"]
                        import math as _math
                        sector = int((_math.degrees(_math.atan2(-ddy, ddx)) + 180 + 22.5) / 45) % 8
                        direction = angle[sector]
                    else:
                        direction = "·"
                    moving_str = motion_state
                else:
                    direction, moving_str = "·", "INIT"
                logger.info("[tracking_object] target='%s' pos=(%.0f%%,%.0f%%) quad=%s offset=(%.0f,%.0f) dist=%.0fpx state=%s dir=%s bbox_area=%.1f%% conf=%.2f yolo_age=%.1fs",
                            state.target_label, screen_x_pct, screen_y_pct, quadrant,
                            dx, dy, offset_mag, moving_str, direction, bbox_ratio * 100,
                            confidence, time.perf_counter() - last_yolo_confirm_t)

                # --- PID continuous-fire with detector-gated trust ---
                now_t = time.perf_counter()
                in_zone = abs(dx) <= w_fr * DEAD_ZONE_YAW_PCT and abs(dy) <= h_fr * DEAD_ZONE_PITCH_PCT
                yolo_age = now_t - last_yolo_confirm_t
                # Ghost-lock recovery: ViT/CSRT sometimes reports ok=True with a
                # bbox larger than the frame (lock dissolved into background).
                # If that persists with no detector confirm, _do_retry instead of
                # breaking — gives one chance to relocate via YOLO/YuNet before
                # giving up the session.
                if bbox_ratio > 0.95 and yolo_age >= 3.0:
                    logger.warning("[ghost-lock] bbox=%.0f%% no-detect=%.1fs → forced retry",
                                   bbox_ratio * 100, yolo_age)
                    if _do_retry():
                        last_yolo_confirm_t = time.perf_counter()
                        continue
                    break
                if in_zone:
                    self._yaw_pid.reset()
                    self._pitch_pid.reset()
                    motion_state = "CENTERED"
                elif yolo_age >= STOP_NO_YOLO_S:
                    logger.warning("[yolo-trust] no YOLO confirm for %.1fs > %.1fs — stopping ghost",
                                   yolo_age, STOP_NO_YOLO_S)
                    break
                elif yolo_age >= TRUST_TRACKER_S and confidence < TRACKER_TRUST_CONF:
                    # Tracker AND detector both unsure — hold servo, don't chase
                    # phantom. If ViT confidence is high we trust the tracker
                    # even without detector confirm (face moving fast often makes
                    # YuNet miss while ViT keeps a good lock).
                    motion_state = "WAIT-YOLO"
                elif (now_t - last_servo_t) >= SERVO_COOLDOWN_S:
                    motion_state = "CHASING"
                    # Yaw sign: dx>0 (object on right) → base_yaw must INCREASE
                    # to chase right (verified empirically vs legacy _fire_gimbal,
                    # log shows camera moving the wrong way when this was negated).
                    yaw_step = self._yaw_pid.update(dx) if abs(dx) > w_fr * DEAD_ZONE_YAW_PCT else 0.0
                    pitch_correction = self._pitch_pid.update(dy) if abs(dy) > h_fr * DEAD_ZONE_PITCH_PCT else 0.0
                    logger.info("[pid-fire] offset=(%.0f,%.0f) → %.0f%%x/%.0f%%y yaw=%.2f pitch=%.2f target='%s'",
                                dx, dy,
                                abs(dx) / w_fr * 100, abs(dy) / h_fr * 100,
                                yaw_step, pitch_correction, state.target_label)
                    t_servo_ms = self._fire_pid(yaw_step, pitch_correction, animation_service)
                    t_servo_acc += t_servo_ms
                    servo_count += 1
                    last_servo_t = now_t
                prev_dx, prev_dy = dx, dy

                # Drain YOLO result queue — re-init tracker ONLY when it has
                # clearly diverged. Blindly reiniting on every YOLO confirm causes
                # ViT to bbox-bloat after re-init, which "teleports" the centroid
                # and lurches the servo (the main cause of jerky tracking).
                try:
                    yolo_bbox = yolo_q.get_nowait()
                    if yolo_bbox is not None:
                        miss_count = 0
                        last_yolo_confirm_t = time.perf_counter()
                        cur_bbox = state.bbox
                        cur_area = (cur_bbox[2] * cur_bbox[3]) if cur_bbox else 0
                        yolo_area = yolo_bbox[2] * yolo_bbox[3]
                        # Center distance between tracker bbox and YOLO bbox
                        cdx = cdy = 0.0
                        if cur_bbox is not None:
                            cdx = (cur_bbox[0] + cur_bbox[2] / 2.0) - (yolo_bbox[0] + yolo_bbox[2] / 2.0)
                            cdy = (cur_bbox[1] + cur_bbox[3] / 2.0) - (yolo_bbox[1] + yolo_bbox[3] / 2.0)
                        center_dist = (cdx ** 2 + cdy ** 2) ** 0.5
                        # Reinit only when truly drifted. For a large bbox (e.g. full person
                        # at 70%+ frame), YOLO and tracker can legitimately disagree on
                        # center by 100–200px frame-to-frame just from how each draws the
                        # bbox edges. Scale the divergence threshold by the smaller bbox
                        # dimension so a 500-wide bbox tolerates ~200px center jitter.
                        cur_min_dim = min(cur_bbox[2], cur_bbox[3]) if cur_bbox else 0
                        diverge_threshold = max(120.0, cur_min_dim * 0.4)
                        bloated = cur_area > 0 and cur_area > yolo_area * 2.0
                        diverged = center_dist > diverge_threshold
                        if bloated or diverged:
                            logger.info("[drift-correct] reinit reason: bloated=%s diverged=%s "
                                        "cur_area=%d yolo_area=%d center_dist=%.0fpx",
                                        bloated, diverged, cur_area, yolo_area, center_dist)
                            ema_dx = ema_dy = None
                            new_tracker = self._create_tracker()
                            if new_tracker is not None:
                                reinit_frame = camera_capture.last_frame
                                if reinit_frame is not None:
                                    try:
                                        ok_r = new_tracker.init(reinit_frame, yolo_bbox)
                                        if ok_r is not False:
                                            state.tracker = new_tracker
                                            state.bbox = yolo_bbox
                                            motion_state = "INIT"
                                            stable_count = 0
                                    except Exception as e:
                                        logger.warning("YOLO re-init failed: %s", e)
                        else:
                            logger.debug("[drift-correct] tracker OK, skipping reinit "
                                         "(cur_area=%d yolo_area=%d center_dist=%.0fpx)",
                                         cur_area, yolo_area, center_dist)
                    else:
                        yolo_miss_count += 1
                        logger.debug("YOLO scan: target not found (%d consecutive)", yolo_miss_count)
                except queue.Empty:
                    pass
                else:
                    if yolo_bbox is not None:
                        yolo_miss_count = 0

                # Force immediate YOLO redetect when object drifts to frame edge —
                # CSRT will lose lock before the normal interval fires.
                if (abs(dx) > w_fr * 0.25 or abs(dy) > h_fr * 0.25) and not yolo_running.is_set():
                    last_yolo_t = 0
                    logger.info("[edge] offset=(%.0f,%.0f) > 25%% frame → force YOLO target='%s'",
                                dx, dy, state.target_label)

                # Fire background YOLO scan every YOLO_REDETECT_S.
                now = time.perf_counter()
                if state.target_label and not yolo_running.is_set() and now - last_yolo_t >= YOLO_REDETECT_S:
                    last_yolo_t = now
                    yolo_running.set()
                    snap = frame.copy()
                    threading.Thread(
                        target=_fire_yolo, args=(snap,), daemon=True, name="yolo-worker"
                    ).start()

                # Log every ~2 seconds.
                frame_count += 1
                fps_elapsed = time.perf_counter() - fps_t0
                if fps_elapsed >= 2.0:
                    csrt_avg = t_csrt_acc / frame_count if frame_count else 0.0
                    servo_avg = t_servo_acc / servo_count if servo_count else 0.0
                    frame_avg = fps_elapsed * 1000 / frame_count if frame_count else 0.0
                    logger.info(
                        "[track-loop] fps=%.1f csrt=%.0fms servo=%.0fms(%d) frame=%.0fms"
                        " offset=(%.0f,%.0f) bbox=%s target='%s'",
                        frame_count / fps_elapsed,
                        csrt_avg, servo_avg, servo_count,
                        frame_avg, dx, dy, state.bbox, state.target_label,
                    )
                    # System metrics snapshot
                    try:
                        import subprocess as _sp
                        cpu = float(open("/proc/loadavg").read().split()[0])
                        mem_info = open("/proc/meminfo").read()
                        mem_total = int(next(l.split()[1] for l in mem_info.splitlines() if "MemTotal" in l))
                        mem_avail = int(next(l.split()[1] for l in mem_info.splitlines() if "MemAvailable" in l))
                        mem_used_pct = (mem_total - mem_avail) / mem_total * 100
                        volt = _sp.check_output(["vcgencmd", "measure_volts", "core"],
                                                stderr=_sp.DEVNULL, text=True).strip()
                        logger.info("[tracking_system] cpu_load1=%.2f ram_used=%.0f%% voltage=%s", cpu, mem_used_pct, volt)
                    except Exception:
                        pass
                    frame_count = 0
                    t_csrt_acc = 0.0
                    t_servo_acc = 0.0
                    servo_count = 0
                    fps_t0 = time.perf_counter()

                if time.perf_counter() - track_start_t > MAX_TRACK_DURATION_S:
                    logger.warning("Tracking timeout after %ds, stopping", MAX_TRACK_DURATION_S)
                    break

                dt = time.perf_counter() - t0
                sleep_time = (1.0 / FAST_LOOP_FPS) - dt
                if sleep_time > 0:
                    time.sleep(sleep_time)

        finally:
            animation_service._tracking_active = False
            animation_service._hold_mode = False
            state.running.clear()

            try:
                # Return to zero at tracking speed — keep velocity+accel low so
                # arm glides back instead of snapping.
                time.sleep(0.8)
                with animation_service.bus_lock:
                    animation_service.robot.send_action({
                        "base_yaw.pos": 0.0,
                        "base_pitch.pos": 0.0,
                        "elbow_pitch.pos": 0.0,
                        "wrist_roll.pos": 0.0,
                        "wrist_pitch.pos": 0.0,
                    })
                logger.info("Tracking ended — arm returned to zero")
                # Restore full speed after arm has started moving to zero.
                time.sleep(1.0)
                with animation_service.bus_lock:
                    if TRACKING_GOAL_VELOCITY > 0:
                        animation_service.robot.bus.sync_write(
                            "Goal_Velocity", {m: 0 for m in _tracking_motors}
                        )
                    animation_service.robot.bus.sync_write(
                        "Acceleration", {m: 254 for m in _tracking_motors}
                    )
            except Exception as e:
                logger.warning("Tracking ended — failed to zero arm: %s", e)

            if not animation_service._running.is_set():
                animation_service._running.set()
                animation_service._event_thread = threading.Thread(
                    target=animation_service._event_loop, daemon=True
                )
                animation_service._event_thread.start()
