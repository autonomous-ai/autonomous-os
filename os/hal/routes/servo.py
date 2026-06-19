"""Servo route handlers — all /servo/* endpoints."""

import csv
import io
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, File, Form, UploadFile

import hal.app_state as state
from hal.safety.policy import min_move_duration
from hal.models import (
    ServoAimRequest,
    ServoAimResponse,
    ServoNudgeRequest,
    ServoMoveRequest,
    ServoMoveResponse,
    ServoPositionResponse,
    ServoRequest,
    ServoStateResponse,
    ServoStatusResponse,
    ServoTrackRequest,
    ServoTrackResponse,
    StatusResponse,
)
from hal.presets import (
    AIM_LEFT,
    AIM_PRESETS,
    AIM_RIGHT,
    SERVO_CMD_PLAY,
)
from hal.drivers.motors.animation_service import RESUME_STARTUP_RAW, STARTUP_MOVE_DURATION, ZERO_RAW

router = APIRouter(tags=["Servo"])

# --- Constants ---

_SERVO_JOINT_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+\.pos$")
_MAX_SERVO_RECORDING_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB
_MAX_SERVO_RECORDING_ROWS = 20000


def _sanitize_recording_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    name = name.strip("_- ")
    if not name:
        raise ValueError("empty recording name")
    return name[:64]


# --- Endpoints ---


@router.get("/servo", response_model=ServoStateResponse)
def get_servo_state():
    """Get available recordings and current animation state."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    return {
        "available_recordings": state.animation_service.get_available_recordings(),
        "current": state.animation_service._current_recording,
    }


@router.post("/servo/upload", response_model=StatusResponse)
async def upload_servo_recording(
    file: UploadFile = File(...),
    recording_name: Optional[str] = Form(None),
):
    """Upload a servo recording CSV and make it available in GET /servo."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")

    orig_filename = file.filename or "recording.csv"
    if orig_filename.lower().endswith(".csv") is False:
        raise HTTPException(400, "upload must be a .csv file")

    rec_name = recording_name or Path(orig_filename).stem
    try:
        rec_name = _sanitize_recording_name(rec_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "empty csv")
    if len(content) > _MAX_SERVO_RECORDING_UPLOAD_BYTES:
        raise HTTPException(
            413, f"csv too large (max {_MAX_SERVO_RECORDING_UPLOAD_BYTES} bytes)"
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "csv must be utf-8 text")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []

    if "timestamp" not in fieldnames:
        raise HTTPException(400, 'missing required column "timestamp"')

    joint_fields = [f for f in fieldnames if f != "timestamp"]
    if not joint_fields:
        raise HTTPException(400, "missing joint columns (expected *.pos fields)")

    invalid_joint_fields = [f for f in joint_fields if not _SERVO_JOINT_FIELD_RE.match(f)]
    if invalid_joint_fields:
        raise HTTPException(
            400, f"invalid joint columns: {invalid_joint_fields}. Expected <name>.pos"
        )

    valid_joints = None
    try:
        if (
            state.animation_service.robot
            and state.animation_service.robot.bus
            and state.animation_service.robot.bus.motors
        ):
            valid_joints = {f"{m}.pos" for m in state.animation_service.robot.bus.motors}
    except Exception:
        valid_joints = None

    if valid_joints is not None:
        unknown = [j for j in joint_fields if j not in valid_joints]
        if unknown:
            raise HTTPException(
                400,
                f"unknown joint columns: {unknown}. Valid: {sorted(valid_joints)}",
            )

    actions: list[dict[str, float]] = []
    for row_idx, row in enumerate(reader):
        if len(actions) >= _MAX_SERVO_RECORDING_ROWS:
            raise HTTPException(
                400, f"too many rows (max {_MAX_SERVO_RECORDING_ROWS})"
            )

        ts_val = row.get("timestamp")
        try:
            _ = float(ts_val)
        except Exception:
            raise HTTPException(400, f"invalid timestamp at row {row_idx + 2}")

        action: dict[str, float] = {}
        for joint in joint_fields:
            v = row.get(joint)
            if v is None or v == "":
                raise HTTPException(400, f"missing value for {joint} at row {row_idx + 2}")
            try:
                action[joint] = float(v)
            except Exception:
                raise HTTPException(400, f"invalid float for {joint} at row {row_idx + 2}")

        actions.append(action)

    recordings_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "recordings")
    Path(recordings_dir).mkdir(parents=True, exist_ok=True)
    csv_path = os.path.join(recordings_dir, f"{rec_name}.csv")

    with open(csv_path, "w", newline="") as f:
        f.write(text if text.endswith("\n") else text + "\n")

    try:
        state.animation_service._recording_cache[rec_name] = actions
    except Exception:
        pass

    return {"status": "ok"}


@router.post("/servo/play", response_model=StatusResponse)
def play_recording(req: ServoRequest):
    """Play a pre-recorded servo animation by name."""
    state.logger.debug("POST /servo/play recording=%s", req.recording)
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if getattr(state.animation_service, "_zero_mode", False) or getattr(state.animation_service, "_hold_mode", False):
        state.logger.debug("servo/play blocked: %s mode active",
                           "zero-hold" if state.animation_service._zero_mode else "hold")
        return {"status": "ok"}
    if not state.animation_service._running.is_set():
        state.animation_service._running.set()
        state.animation_service._event_thread = threading.Thread(
            target=state.animation_service._event_loop, daemon=True
        )
        state.animation_service._event_thread.start()
        state.logger.info("Animation event loop restarted via /servo/play")
    t0 = time.perf_counter()
    state.animation_service.dispatch(SERVO_CMD_PLAY, req.recording)
    state.logger.debug("servo dispatch took %.1fms", (time.perf_counter() - t0) * 1000)
    return {"status": "ok"}


@router.post("/servo/resume", response_model=StatusResponse)
def resume_servos():
    """Exit zero-hold mode and resume normal animation loop (plays idle)."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    state.animation_service._zero_mode = False
    state.animation_service._hold_mode = False
    # Stop the event loop before raw bus moves to prevent bus contention
    state.animation_service._running.clear()
    if state.animation_service._event_thread and state.animation_service._event_thread.is_alive():
        state.animation_service._event_thread.join(timeout=3.0)
    # Re-enable torque and reconfigure servos (release left torque disabled).
    # REQUIRED — without this the arm stays limp and idle can't move it.
    try:
        state.animation_service._configure_servos_raw()
    except Exception as e:
        state.logger.warning("resume: raw configure failed: %s", e)
    # Sync state from the (released/folded) hardware pose so the idle dispatch
    # below interpolates the lift FROM where the arm actually is — no jerk.
    state.animation_service._sync_state_from_hardware()
    # Let the idle dispatch lift the arm via normal interpolation instead of a
    # separate 5s startup ramp (PR 174 added that move_to_raw, which made resume
    # take ~5-8s). _resume_duration controls that single folded→idle lift; use
    # the normal move duration (~2s). NOTE: PR 174's 5s ramp was for smoothness
    # on the new servo — if the lift looks jerky, raise this back up.
    state.animation_service._resume_duration = state.animation_service.duration
    # Restart event loop
    state.animation_service._running.set()
    state.animation_service.dispatch(SERVO_CMD_PLAY, state.animation_service.idle_recording)
    state.animation_service._event_thread = threading.Thread(
        target=state.animation_service._event_loop, daemon=True
    )
    state.animation_service._event_thread.start()
    state.logger.info("Servo resumed from zero-hold mode")
    return {"status": "ok"}


@router.post("/servo/hold", response_model=StatusResponse)
def hold_servos():
    """Hold current pose -- suppress idle/ambient animations, torque stays ON."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    state.animation_service._hold_mode = True
    state.logger.info("Servo hold mode activated -- idle suppressed, emotions still allowed")
    return {"status": "ok"}


@router.post("/servo/move", response_model=ServoMoveResponse)
def move_servo(req: ServoMoveRequest):
    """Send joint positions to servo motors with smooth interpolation."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")
    valid_joints = {f"{m}.pos" for m in state.animation_service.robot.bus.motors}
    unknown = [j for j in req.positions if j not in valid_joints]
    if unknown:
        raise HTTPException(
            400, f"Unknown joints: {unknown}. Valid: {sorted(valid_joints)}"
        )

    # Safety gate (SAFETY.md motion) — presence-driven, like light/audio: a
    # declared motion.max_speed is enforced, an absent one is pass-through (no
    # bounds → the move runs unrestricted, that is the off state, not a refusal).
    # stop/release/zero are recovery actions and are never gated.

    # Speed ceiling (motion.max_speed): read the current pose and stretch the
    # duration so no joint exceeds it. Best-effort read — if it fails we fall back
    # to the requested duration (the move still happens, just unclamped this once).
    current = {}
    try:
        with state.animation_service.bus_lock:
            current = {
                k: v for k, v in state.animation_service.robot.get_observation().items()
                if k.endswith(".pos")
            }
    except Exception as e:
        state.logger.warning("move: could not read current pose for speed clamp: %s", e)
    eff_duration = min_move_duration(state.safety_policy, req.positions, current, req.duration)

    errors = {}

    try:
        # move_and_hold preempts any in-flight emotion animation so it can't
        # overwrite the commanded pose (race fix); it also keeps the pose afterwards.
        state.animation_service.move_and_hold(req.positions, duration=eff_duration)
    except Exception as e:
        errors["move"] = str(e)

    try:
        with state.animation_service.bus_lock:
            obs = state.animation_service.robot.get_observation()
        for joint, target in req.positions.items():
            actual = obs.get(joint)
            if actual is not None:
                error = abs(actual - target)
                if error > 5.0:
                    errors[joint] = (
                        f"position error {error:.1f} deg (target={target:.1f}, actual={actual:.1f})"
                    )
    except Exception as e:
        errors["read_position"] = str(e)

    return {
        "status": "error" if "move" in errors else "ok",
        "requested": req.positions,
        "clamped": req.positions,
        "duration": eff_duration,  # may exceed req.duration when speed-clamped
        "errors": errors if errors else None,
    }


@router.post("/servo/zero", response_model=StatusResponse)
def zero_servos():
    """Move all servos to 0 deg and hold (torque stays ON)."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")
    state.animation_service._zero_mode = True
    state.animation_service._running.clear()
    if state.animation_service._event_thread and state.animation_service._event_thread.is_alive():
        state.animation_service._event_thread.join(timeout=3.0)
    try:
        state.animation_service._configure_servos_raw()
    except Exception as e:
        state.logger.warning("zero: raw configure failed: %s", e)
    try:
        state.animation_service.move_to_raw(ZERO_RAW, duration=2.0)
    except Exception as e:
        state.logger.warning(f"Could not move to zero: {e}")
    state.animation_service._sync_state_from_hardware()
    return {"status": "ok"}


@router.post("/servo/release", response_model=StatusResponse)
def release_servos():
    """Move servos to idle position then disable torque (safe release)."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")
    state.animation_service._running.clear()
    if state.animation_service._event_thread and state.animation_service._event_thread.is_alive():
        state.animation_service._event_thread.join(timeout=3.0)
    # Gravity-rest pose for lumi_final in raw encoder units.
    # Pre-computed from calibration JSON: raw = round(deg * 4095/360 + mid)
    # where mid = (range_min + range_max) / 2.
    # base_pitch/elbow_pitch/wrist_pitch exceed calibrated range_min so we
    # use move_to_raw (direct STS3215 writes) to bypass lerobot's software clamp.
    rest_raw = {
        "base_yaw":    2075,  #   3.00° — mid=2041.5
        "base_pitch":  1456,  # -75.26° — mid=2312.5
        "elbow_pitch": 1626,  # -65.02° — mid=2366.5
        "wrist_roll":  2070,  #   0.00° — mid=2070.0
        "wrist_pitch": 2108,  # -42.16° — mid=2588.0
    }
    try:
        state.animation_service.move_to_raw(rest_raw, duration=4.0)
    except Exception as e:
        state.logger.warning(f"Could not move to rest before release: {e}")
    # Poll PRESENT_POSITION until all joints physically reach rest_raw before cutting torque.
    # move_to_raw only writes GOAL_POSITION — under load the servo lags the command.
    _PRESENT_REG = 56
    _tol_raw = 23  # ~2 degrees (4095/360 * 2)
    _bus = state.animation_service.robot.bus
    _deadline = time.perf_counter() + 3.0
    while time.perf_counter() < _deadline:
        with state.animation_service.bus_lock:
            actual = {}
            for _name, _motor in _bus.motors.items():
                _data, _result, _ = _bus.packet_handler.read2ByteTxRx(
                    _bus.port_handler, _motor.id, _PRESENT_REG
                )
                if _result == 0:
                    actual[_name] = _data
        if all(abs(actual.get(k, 0) - v) <= _tol_raw for k, v in rest_raw.items()):
            break
        time.sleep(0.05)
    else:
        state.logger.warning("rest_raw not reached within 3s; releasing torque anyway")
    bus = state.animation_service.robot.bus
    errors = {}
    with state.animation_service.bus_lock:
        for motor_name in bus.motors:
            try:
                bus.write("Torque_Enable", motor_name, 0)
            except Exception as e:
                errors[motor_name] = str(e)
    if errors:
        state.logger.warning(f"Servo release errors (offline?): {errors}")
    return {"status": "ok"}


@router.get("/servo/position", response_model=ServoPositionResponse)
def get_servo_position():
    """Read current servo joint positions."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")
    try:
        with state.animation_service.bus_lock:
            obs = state.animation_service.robot.get_observation()
        positions = {k: v for k, v in obs.items() if k.endswith(".pos")}
        return {"positions": positions}
    except Exception as e:
        raise HTTPException(500, f"Failed to read position: {e}")


@router.get("/servo/status", response_model=ServoStatusResponse)
def get_servo_status():
    """Ping each servo and return per-joint online/offline status with angle."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")
    bus = state.animation_service.robot.bus
    ph = bus.port_handler
    pk = bus.packet_handler
    from scservo_sdk import COMM_SUCCESS

    servos = {}
    with state.animation_service.bus_lock:
        for motor_name, motor_obj in bus.motors.items():
            key = f"{motor_name}.pos"
            sid = motor_obj.id
            detail = {"id": sid, "angle": None, "online": False, "error": None}
            try:
                _, result, _ = pk.ping(ph, sid)
                if result != COMM_SUCCESS:
                    detail["error"] = "no status packet"
                else:
                    detail["online"] = True
                    try:
                        pos = bus.read("Present_Position", motor_name)
                        detail["angle"] = float(pos)
                    except Exception as e:
                        detail["error"] = f"read failed: {e}"
            except Exception as e:
                detail["error"] = str(e)
            servos[key] = detail
    return {"servos": servos}


@router.get("/servo/aim")
def list_aim_directions():
    """List available aim directions."""
    return {"directions": list(AIM_PRESETS.keys())}


@router.post("/servo/aim", response_model=ServoAimResponse)
def aim_servo(req: ServoAimRequest):
    """Aim the device head to a named direction."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")

    preset = AIM_PRESETS.get(req.direction)
    if preset is None:
        available = list(AIM_PRESETS.keys())
        raise HTTPException(
            400, f"Unknown direction '{req.direction}'. Available: {available}"
        )

    was_running = state.animation_service._running.is_set()
    if was_running:
        state.animation_service._running.clear()
        if state.animation_service._event_thread and state.animation_service._event_thread.is_alive():
            state.animation_service._event_thread.join(timeout=2.0)

    try:
        with state.animation_service.bus_lock:
            obs = state.animation_service.robot.get_observation()
        current = {k: v for k, v in obs.items() if k.endswith(".pos")}

        if req.direction in (AIM_LEFT, AIM_RIGHT):
            positions = {**current, "base_yaw.pos": preset["base_yaw.pos"]}
        else:
            positions = {**preset, "base_yaw.pos": current.get("base_yaw.pos", preset["base_yaw.pos"])}

        if req.duration > 0:
            state.animation_service.move_to(positions, duration=req.duration)
        else:
            with state.animation_service.bus_lock:
                state.animation_service.robot.send_action(positions)
        return {"status": "ok", "direction": req.direction, "positions": positions}
    except Exception as e:
        raise HTTPException(500, f"Servo aim failed: {e}")
    finally:
        if was_running and not state.animation_service._running.is_set():
            hold_pos = state.animation_service._current_state
            if hold_pos:
                state.animation_service._current_recording = "__aim_hold__"
                state.animation_service._current_actions = [hold_pos]
                state.animation_service._current_frame_index = 0
                state.animation_service._hold_until = time.time() + 5.0
            state.animation_service._running.set()
            state.animation_service._event_thread = threading.Thread(
                target=state.animation_service._event_loop, daemon=True
            )
            state.animation_service._event_thread.start()
            if not hold_pos:
                state.animation_service.dispatch(SERVO_CMD_PLAY, state.animation_service.idle_recording)


@router.post("/servo/nudge", response_model=ServoAimResponse)
def nudge_servo(req: ServoNudgeRequest):
    """Move servo by relative degrees from current position."""
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.animation_service.robot:
        raise HTTPException(503, "Servo robot not connected")

    try:
        with state.animation_service.bus_lock:
            obs = state.animation_service.robot.get_observation()
        current = {k: v for k, v in obs.items() if k.endswith(".pos")}

        positions = dict(current)
        if req.yaw != 0:
            positions["base_yaw.pos"] = current.get("base_yaw.pos", 0) + req.yaw
        if req.pitch != 0:
            positions["base_pitch.pos"] = current.get("base_pitch.pos", 0) + req.pitch

        # Safety speed cap (SAFETY.md motion.max_speed) — stretch the duration so no
        # joint exceeds the deg/s ceiling. nudge previously sent at duration=0 (an
        # instant jump = unbounded speed); clamp it the same way /servo/move does.
        eff_duration = min_move_duration(state.safety_policy, positions, current, req.duration)
        # move_and_hold preempts any in-flight emotion animation so it can't
        # overwrite the nudged pose (race fix); it also keeps the pose afterwards.
        state.animation_service.move_and_hold(positions, duration=eff_duration)

        return {"status": "ok", "direction": f"nudge yaw={req.yaw} pitch={req.pitch}", "positions": positions}
    except Exception as e:
        raise HTTPException(500, f"Servo nudge failed: {e}")


@router.post("/servo/track", response_model=ServoTrackResponse)
def start_tracking(req: ServoTrackRequest):
    """Start tracking an object by bounding box. Servo follows the object in real-time."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")
    if not state.animation_service:
        raise HTTPException(503, "Servo not available")
    if not state.camera_capture:
        raise HTTPException(503, "Camera not available")

    bbox = tuple(req.bbox) if req.bbox else None
    ok = state.tracker_service.start(
        bbox=bbox,
        target_label=req.target,
        camera_capture=state.camera_capture,
        animation_service=state.animation_service,
    )
    if not ok:
        raise HTTPException(400, state.tracker_service.last_error or "Failed to initialize tracker")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": True,
        "target": s.get("target"),
        "bbox": s.get("bbox"),
        "confidence": s.get("confidence"),
    }


@router.post("/servo/track/stop", response_model=ServoTrackResponse)
def stop_tracking():
    """Stop the current tracking session."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")

    state.tracker_service.stop()
    return {"status": "ok", "tracking": False}


@router.get("/servo/track", response_model=ServoTrackResponse)
def get_tracking_status():
    """Get current tracking status."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": s["tracking"],
        "target": s["target"],
        "bbox": s["bbox"],
        "confidence": s.get("confidence"),
    }


@router.post("/servo/track/update", response_model=ServoTrackResponse)
def update_tracking_bbox(req: ServoTrackRequest):
    """Re-initialize tracker with a new bounding box."""
    if not state.tracker_service:
        raise HTTPException(503, "Tracker service not available")
    if not state.tracker_service.is_tracking:
        raise HTTPException(400, "No active tracking session")

    bbox = tuple(req.bbox)
    ok = state.tracker_service.update_bbox(bbox, camera_capture=state.camera_capture)
    if not ok:
        raise HTTPException(400, "Failed to re-initialize tracker")

    s = state.tracker_service.status
    return {
        "status": "ok",
        "tracking": True,
        "target": s.get("target"),
        "bbox": list(bbox),
    }
