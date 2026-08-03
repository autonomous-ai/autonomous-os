"""Reachy Mini motion driver — wraps Pollen's `reachy_mini` SDK.

Satisfies the MotionService protocol (hal/drivers/motors/base.py) so
routes/servo.py works unchanged on Reachy Mini.

Architecture: Pollen ships a daemon on the robot's Pi that owns hardware I/O,
safety clamping, and the control loop; this driver is a thin client streaming
targets to it (REST/WS on localhost:8000). Unlike the feetech backend there is
no client-side per-frame animation event loop — the daemon holds poses by
itself, so several protocol methods (ensure_running, freeze) reduce to
flags/no-ops. The one exception is music: the play thread repeats the groove
move until music_stop, mirroring the feetech backend (see _play_recording).

Joint model exposed to HAL (degrees + millimeters, matching the deg-based
safety policy and route error checks; the SDK itself speaks radians + meters):

    head_x.pos / head_y.pos / head_z.pos      head translation, mm
    head_roll.pos / head_pitch.pos / head_yaw.pos   head rotation, deg
    body_yaw.pos                              body rotation, deg
    antenna_left.pos / antenna_right.pos      antennas, deg

DRAFT: written against the public reachy-mini SDK docs (v1.9) before hardware
arrival. Everything tagged TODO(spike) must be verified on the real robot.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Dict, List, Optional, Set

import numpy as np
from scipy.spatial.transform import Rotation

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

# SDK motor ids, in get_current_joint_positions() head-list order.
_HEAD_MOTOR_IDS = (
    "body_rotation",
    "stewart_1", "stewart_2", "stewart_3", "stewart_4", "stewart_5", "stewart_6",
)

logger = logging.getLogger(__name__)

# --- Joint keys ---

_HEAD_KEYS = (
    "head_x.pos", "head_y.pos", "head_z.pos",
    "head_roll.pos", "head_pitch.pos", "head_yaw.pos",
)
_ANTENNA_KEYS = ("antenna_left.pos", "antenna_right.pos")
_BODY_KEY = "body_yaw.pos"
JOINT_KEYS: Set[str] = {*_HEAD_KEYS, *_ANTENNA_KEYS, _BODY_KEY}

# Daemon-enforced limits (the daemon clamps to the nearest valid pose; these
# are documented here for reference): head pitch/roll ±40°, head yaw ±180°,
# body yaw ±160°, head-vs-body yaw delta ≤ 65°.

# Default HF move library preloaded by the daemon.
_EMOTES_DATASET = "pollen-robotics/reachy-mini-emotions-library"

# HAL recording name (CSV stem on lamp) → Pollen HF move name.
# Keys use preset constants from hal.presets; unmatched names are tried verbatim
# (in case the caller already uses HF names).
import hal.presets as P

# Aim table — same direction vocabulary as hal/presets AIM_PRESETS, values are
# Reachy head kinematics. Convention: +yaw = left, +pitch = up (ROS-style).
# TODO(spike): verify sign convention and tune angles on the real robot.
_AIM_TARGETS: Dict[str, Dict[str, float]] = {
    P.AIM_CENTER: {"head_yaw.pos": 0.0, "head_pitch.pos": 0.0, "head_z.pos": 0.0},
    P.AIM_DESK:   {"head_yaw.pos": 0.0, "head_pitch.pos": -30.0, "head_z.pos": 0.0},
    P.AIM_WALL:   {"head_yaw.pos": 0.0, "head_pitch.pos": 20.0, "head_z.pos": 0.0},
    P.AIM_LEFT:   {"head_yaw.pos": 40.0},
    P.AIM_RIGHT:  {"head_yaw.pos": -40.0},
    P.AIM_UP:     {"head_pitch.pos": 30.0},
    P.AIM_DOWN:   {"head_pitch.pos": -35.0},
    P.AIM_USER:   {"head_yaw.pos": 0.0, "head_pitch.pos": 15.0},
}

_MOVE_MAP: Dict[str, str] = {
    # emotions
    P.SERVO_CURIOUS:       "curious1",
    P.SERVO_HAPPY_WIGGLE:  "cheerful1",
    P.SERVO_SAD:           "sad1",
    P.SERVO_THINKING_DEEP: "thoughtful1",
    P.SERVO_IDLE:          "serenity1",
    P.SERVO_EXCITED:       "enthusiastic1",
    P.SERVO_SHY:           "shy1",
    P.SERVO_SHOCK:         "surprised1",
    P.SERVO_LISTENING:     "attentive1",
    P.SERVO_LAUGH:         "laughing1",
    P.SERVO_CONFUSED:      "confused1",
    P.SERVO_SLEEPY:        "tired1",
    P.SERVO_GREETING:      "welcoming1",
    P.SERVO_GOODBYE:       "resigned1",
    P.SERVO_NOD:           "yes1",
    P.SERVO_ACKNOWLEDGE:   "understanding1",
    P.SERVO_STRETCHING:    "relief1",
    P.SERVO_SCANNING:      "inquiring1",
    P.SERVO_HEADSHAKE:     "no1",
    P.SERVO_WAKE_UP:       "enthusiastic2",
    # music grooves — Pollen ships 3 dance moves, spread across the 8 styles
    P.SERVO_MUSIC_GROOVE:    "dance1",
    P.SERVO_MUSIC_JAZZ:      "dance2",
    P.SERVO_MUSIC_CLASSICAL: "dance3",
    P.SERVO_MUSIC_HIPHOP:    "dance1",
    P.SERVO_MUSIC_ROCK:      "dance2",
    P.SERVO_MUSIC_WALTZ:     "dance3",
    P.SERVO_MUSIC_CHILL:     "dance1",
    P.SERVO_MUSIC_HYPE:      "dance2",
}

_MIN_MOVE_DURATION_S = 0.1

# The SDK client holds one long-lived connection to the daemon. Anything that
# restarts the daemon — an OS reboot, `systemctl restart reachy-mini-daemon`, an
# update — leaves this side holding a dead socket that only reports itself when
# the next command is issued ("Lost connection with the server"). Without a
# reconnect the first command after every such event is lost and surfaces as a
# 500, which reads like the robot refusing to move.
_RECONNECT_MARKERS = (
    "lost connection",
    "connection closed",
    "not connected",
    "broken pipe",
    "connection reset",
)


def _is_disconnect(err: Exception) -> bool:
    return any(m in str(err).lower() for m in _RECONNECT_MARKERS)


class ReachyMotionService:
    """MotionService implementation backed by the Pollen reachy_mini daemon."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None):
        # HAL runs on the robot's own Pi, so the daemon is local by default.
        self._host = host or os.getenv("REACHY_DAEMON_HOST", "localhost")
        self._port = int(port or os.getenv("REACHY_DAEMON_PORT", "8000"))
        self._mini: Optional[ReachyMini] = None
        # The SDK client documents no thread-safety guarantee — one writer.
        self._lock = threading.RLock()
        # Last commanded targets; fallback state when the daemon can't be read.
        self._target: Dict[str, float] = {k: 0.0 for k in JOINT_KEYS}
        self._suppressed = False
        self._frozen = False
        self._moves = None          # lazy RecordedMoves loader (None=untried, False=failed)
        self._play_thread: Optional[threading.Thread] = None
        # Bumped on every play request; the play thread stops repeating once its
        # own generation is stale (a newer play owns the servo).
        self._play_gen = 0
        # Music groove state — the only thing that repeats on this backend.
        self._music_playing = False
        self._music_recording: Optional[str] = None
        # Read by GET /servo alongside get_available_recordings().
        self._current_recording: Optional[str] = None

    # --- Lifecycle ---

    def start(self) -> None:
        with self._lock:
            # media_backend="no_media": HAL owns camera/audio via its own
            # drivers — the SDK must not grab them too.
            self._mini = ReachyMini(
                host=self._host, port=self._port, media_backend="no_media"
            )
            self._enable_motors()
            try:
                self._mini.wake_up()
            except Exception as e:
                logger.warning("[reachy] wake_up failed (continuing): %s", e)
        logger.info("[reachy] connected to daemon at %s:%s", self._host, self._port)

    def _enable_motors(self) -> None:
        """Put torque on the motors (caller holds _lock).

        The robot boots unpowered so its head can be posed by hand, and
        connecting does not change that: `wake_up()` is a goto plus a sound, and
        `goto_target` against a torque-off robot is accepted and does nothing.
        Nothing else in the connect path sets torque — before this, the only
        caller of enable_motors was resume(), which nothing invokes at startup.

        The failure it caused is invisible from every status surface: start()
        logs "connected", /health reports servo true, /servo/status shows all
        nine joints online, /servo/play returns 200, and the robot sits still.
        It looked like the robot only obeyed once Pollen's own app had been
        opened — because connecting there is what enabled the motors.
        Measured on a Wireless unit, same `yes1` move: max joint delta 0.014 rad
        (sensor noise) with torque off, 0.934 rad with it on.

        Unconditional on purpose: there is no persisted "released" state to
        honour (release() is a live call, with no sidecar), and it matches the
        feetech backend, where connecting the bus powers the servos.
        """
        if self._mini is None:
            return
        try:
            self._mini.enable_motors()
        except Exception as e:
            logger.warning("[reachy] enable_motors failed — robot will not move: %s", e)

    def stop(self, timeout: float = 5.0) -> None:
        # Clear music first: a groove that is still flagged as playing would
        # start its next repeat right after cancel_move and outlive the join.
        self._music_playing = False
        self._music_recording = None
        self._cancel_move()
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=timeout)
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.goto_sleep()
            except Exception as e:
                logger.warning("[reachy] goto_sleep on stop failed: %s", e)
            # ReachyMini has no public close(); cleanup mirrors its __exit__.
            try:
                self._mini.client.disconnect()
            except Exception as e:
                logger.warning("[reachy] client.disconnect failed: %s", e)
            self._mini = None

    @property
    def is_connected(self) -> bool:
        return self._mini is not None

    # --- Animation / event dispatch ---

    def dispatch(self, event_type: str, payload: Any) -> None:
        if event_type == P.SERVO_CMD_PLAY and payload:
            self._play_recording(str(payload))
        elif event_type == P.SERVO_CMD_MUSIC_START:
            self._start_music(str(payload) if payload else P.SERVO_MUSIC_GROOVE)
        elif event_type == P.SERVO_CMD_MUSIC_STOP:
            self._stop_music()
        else:
            # Idle/ambient motion stays daemon-owned — no client-side loop.
            logger.debug("[reachy] dispatch %s ignored", event_type)

    def _start_music(self, recording: str) -> None:
        """Groove until music_stop — the move repeats instead of playing once.

        Matches the feetech backend (animation_service._handle_music_start):
        a single dance move is a few seconds long, so without the repeat the
        robot dances once and then sits still for the rest of the track.
        """
        logger.info("[reachy] music groove '%s' — repeating until music_stop", recording)
        self._music_recording = recording
        self._music_playing = True
        self._play_recording(recording)

    def _stop_music(self) -> None:
        """Stop grooving immediately — cancel the in-flight repeat."""
        if self._music_playing:
            logger.info("[reachy] music groove stopped")
        self._music_playing = False
        self._music_recording = None
        self._cancel_move()

    def _next_groove(self, played: str, gen: int, failed: bool = False) -> Optional[str]:
        """Recording the play thread runs next, or None to end the thread.

        Keeps the groove alive across everything that ends a single pass —
        including a move that could not be played. The agent does send names
        the HF library has no move for (`dance` instead of `dance1`); dropping
        the groove there left the robot still for the rest of the track, while
        the feetech backend just ignores the bad name and keeps looping.
        """
        if gen != self._play_gen:
            return None                       # a newer play owns the servo now
        if not self._music_playing:
            return None
        if self._suppressed or self._frozen:
            logger.info(
                "[reachy] groove interrupted — %s",
                "frozen (camera)" if self._frozen else "suppressed",
            )
            return None
        groove = self._music_recording
        if not groove:
            return None
        if failed and played == groove:
            logger.warning("[reachy] groove '%s' keeps failing — stopping the repeat", groove)
            return None
        return groove

    def get_available_recordings(self) -> List[str]:
        """Playable names, reported in HAL vocabulary wherever one exists.

        GET /servo returns this list next to `current`, and `current` is the
        HAL recording name (`music_groove`), not the HF move the daemon
        actually played (`dance1`). Listing raw HF names put the two fields in
        different vocabularies: the web monitor highlights the entry matching
        `current`, so nothing ever highlighted and the same behavior appeared
        under two names. Mapped moves are listed under their HAL name; the rest
        of the library is listed verbatim and stays playable — _play_recording
        passes unknown names straight through.
        """
        moves = self._recorded_moves()
        if moves is None:
            return []
        try:
            hf_moves = set(moves.list_moves())
        except Exception as e:
            logger.warning("[reachy] list_moves failed: %s", e)
            return []
        mapped = {hal for hal, hf in _MOVE_MAP.items() if hf in hf_moves}
        return sorted(mapped | (hf_moves - set(_MOVE_MAP.values())))

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        # CSV joint recordings are a feetech-backend concept; Reachy moves are
        # HF datasets played by the daemon. TODO(spike): translate CSV rows to
        # a Move if per-device uploaded animations turn out to matter here.
        logger.warning("[reachy] add_recording('%s') not supported on this backend", name)

    def ensure_running(self) -> None:
        # The daemon runs the control loop; nothing to restart client-side.
        pass

    @property
    def is_suppressed(self) -> bool:
        return self._suppressed

    # --- Freeze (camera stabilization) ---

    def freeze(self) -> None:
        self._frozen = True
        self._cancel_move()

    def unfreeze(self) -> None:
        self._frozen = False
        # freeze() cancels the in-flight pass, which ends the groove loop; the
        # feetech backend only pauses servo writes, so resume the groove here
        # instead of leaving the robot still for the rest of the track.
        if self._music_playing and self._music_recording:
            self._play_recording(self._music_recording)

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    # --- Motion primitives ---

    def move_to(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        merged = {**self._current_or_target(), **target_positions}
        self._goto(merged, max(duration, _MIN_MOVE_DURATION_S))

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = 2.0) -> None:
        # The daemon holds the last commanded pose by itself; identical to
        # move_to on this backend (no idle loop to fight).
        self.move_to(target_positions, duration)

    def get_joint_names(self) -> Set[str]:
        return set(JOINT_KEYS)

    def get_positions(self) -> Dict[str, float]:
        with self._lock:
            if self._mini is None:
                return dict(self._target)
            try:
                pose = self._mini.get_current_head_pose()  # 4x4, meters/rad
                xyz = np.asarray(pose)[:3, 3] * 1000.0
                rpy = Rotation.from_matrix(np.asarray(pose)[:3, :3]).as_euler(
                    "xyz", degrees=True
                )
                # head list order: body_rotation, stewart_1..6 (rad);
                # antenna order is [right, left] in the SDK.
                head_joints, ant = self._mini.get_current_joint_positions()
                return {
                    "head_x.pos": float(xyz[0]),
                    "head_y.pos": float(xyz[1]),
                    "head_z.pos": float(xyz[2]),
                    "head_roll.pos": float(rpy[0]),
                    "head_pitch.pos": float(rpy[1]),
                    "head_yaw.pos": float(rpy[2]),
                    "antenna_right.pos": math.degrees(float(ant[0])),
                    "antenna_left.pos": math.degrees(float(ant[1])),
                    _BODY_KEY: math.degrees(float(head_joints[0])),
                }
            except Exception as e:
                logger.warning("[reachy] get_positions read failed, using last target: %s", e)
                return dict(self._target)

    def send_positions(self, positions: Dict[str, float]) -> None:
        merged = {**self._current_or_target(), **positions}
        head, antennas, body_yaw = self._compose(merged)
        self._call(
            "set_target",
            lambda m: m.set_target(head=head, antennas=antennas, body_yaw=body_yaw),
        )
        self._target = merged

    # --- Postures & modes ---

    def zero_pose(self) -> None:
        # Suppress before cancelling: the flag is what stops a groove repeat
        # from starting the next pass and fighting the move to zero.
        self._suppressed = True
        self._cancel_move()
        self._goto({k: 0.0 for k in JOINT_KEYS}, 2.0)

    def release(self) -> Dict[str, str]:
        errors: Dict[str, str] = {}
        self._suppressed = True  # before cancel — stops a groove repeat (see zero_pose)
        self._cancel_move()
        with self._lock:
            mini = self._require_mini()
            try:
                mini.goto_sleep()
            except Exception as e:
                errors["sleep"] = str(e)
            try:
                mini.disable_motors()
            except Exception as e:
                errors["torque"] = str(e)
        self._suppressed = True
        return errors

    def resume(self) -> None:
        with self._lock:
            mini = self._require_mini()
            try:
                mini.enable_motors()
            except Exception as e:
                logger.warning("[reachy] enable_motors failed: %s", e)
            try:
                mini.wake_up()
            except Exception as e:
                logger.warning("[reachy] wake_up failed: %s", e)
        self._suppressed = False

    def hold(self, explicit: bool = False) -> None:
        self._suppressed = True  # before cancel — stops a groove repeat (see zero_pose)
        self._cancel_move()

    def joint_status(self) -> Dict[str, dict]:
        with self._lock:
            head, antennas = self._require_mini().get_current_joint_positions()  # rad
        status: Dict[str, dict] = {}
        # Names match the SDK motor ids (enable_motors/disable_motors vocabulary).
        for i, (name, angle) in enumerate(zip(_HEAD_MOTOR_IDS, head)):
            status[name] = {
                "online": True, "id": i + 1, "angle": round(math.degrees(float(angle)), 1),
            }
        for i, (name, angle) in enumerate(zip(("right_antenna", "left_antenna"), antennas)):
            status[name] = {
                "online": True, "id": len(_HEAD_MOTOR_IDS) + i + 1,
                "angle": round(math.degrees(float(angle)), 1),
            }
        return status

    # --- Aim & nudge ---

    def aim(self, direction: str, duration: float, current_positions: Dict[str, float],
            safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        target = _AIM_TARGETS.get(direction)
        if target is None:
            # Unknown direction (LLM reached for a non-preset word, e.g. "front")
            # — aim the neutral center pose instead of failing the HW node.
            logger.warning("[reachy] unknown aim direction %r — defaulting to center", direction)
            target = _AIM_TARGETS[P.AIM_CENTER]
        positions = {**current_positions, **target}
        eff = min_move_duration(safety_policy, positions, current_positions, duration)
        self._goto({**self._current_or_target(), **positions}, max(eff, _MIN_MOVE_DURATION_S))
        return positions

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: Any) -> Dict[str, float]:
        from hal.safety.policy import min_move_duration

        positions = dict(current_positions)
        if yaw != 0:
            positions["head_yaw.pos"] = current_positions.get("head_yaw.pos", 0.0) + yaw
        if pitch != 0:
            positions["head_pitch.pos"] = current_positions.get("head_pitch.pos", 0.0) + pitch

        eff = min_move_duration(safety_policy, positions, current_positions, duration)
        self._goto({**self._current_or_target(), **positions}, max(eff, _MIN_MOVE_DURATION_S))
        return positions

    # --- Internals ---

    def _reconnect(self) -> None:
        """Drop the dead client and dial the daemon again (caller holds _lock)."""
        try:
            if self._mini is not None:
                self._mini.client.disconnect()
        except Exception:
            pass
        self._mini = ReachyMini(host=self._host, port=self._port, media_backend="no_media")
        # Torque again: whatever killed the connection (a daemon restart, an
        # update) dropped the motors back to their unpowered boot state too, so
        # a reconnect without this hands back a client that reports healthy and
        # moves nothing.
        self._enable_motors()
        logger.info("[reachy] reconnected to daemon at %s:%s", self._host, self._port)

    def _call(self, what: str, fn):
        """Run a daemon call, reconnecting once if the connection went stale.

        Retries only on disconnect markers: a clamp rejection or a bad pose is a
        real error and must not be replayed against a fresh connection.
        """
        with self._lock:
            self._require_mini()
            try:
                return fn(self._mini)
            except Exception as e:
                if not _is_disconnect(e):
                    raise
                logger.warning("[reachy] %s: connection lost (%s) — reconnecting", what, e)
                self._reconnect()
                return fn(self._mini)

    def _require_mini(self) -> ReachyMini:
        if self._mini is None:
            raise RuntimeError("Reachy daemon not connected")
        return self._mini

    def _current_or_target(self) -> Dict[str, float]:
        """Best-effort current pose, falling back to the last commanded target."""
        try:
            return self.get_positions()
        except Exception:
            return dict(self._target)

    def _compose(self, positions: Dict[str, float]):
        """HAL joint dict (deg/mm) → SDK (4x4 head pose, antennas rad, body_yaw rad)."""
        head = create_head_pose(
            x=positions["head_x.pos"], y=positions["head_y.pos"], z=positions["head_z.pos"],
            roll=positions["head_roll.pos"], pitch=positions["head_pitch.pos"],
            yaw=positions["head_yaw.pos"],
            degrees=True, mm=True,
        )
        # SDK antenna order is [right, left].
        antennas = np.deg2rad(
            [positions["antenna_right.pos"], positions["antenna_left.pos"]]
        )
        body_yaw = math.radians(positions[_BODY_KEY])
        return head, antennas, body_yaw

    def _goto(self, positions: Dict[str, float], duration: float) -> None:
        head, antennas, body_yaw = self._compose(positions)
        self._call(
            "goto_target",
            lambda m: m.goto_target(
                head=head, antennas=antennas, body_yaw=body_yaw, duration=duration
            ),
        )
        self._target = dict(positions)

    def _cancel_move(self) -> None:
        with self._lock:
            if self._mini is None:
                return
            try:
                self._mini.cancel_move()
            except Exception as e:
                logger.debug("[reachy] cancel_move: %s", e)

    def _recorded_moves(self):
        """Lazy-load the HF emotes library. Returns the loader or None."""
        if self._moves is None:
            try:
                from reachy_mini.motion.recorded_move import RecordedMoves
                self._moves = RecordedMoves(_EMOTES_DATASET)
            except Exception as e:
                logger.warning("[reachy] recorded moves unavailable: %s", e)
                self._moves = False
        return self._moves or None

    def _play_move_once(self, move: Any, name: str) -> None:
        """Play one pass of a recorded move (blocking), reconnecting once."""
        with self._lock:
            mini = self._require_mini()
        # play_move blocks for the move duration — keep it out of _lock so
        # state reads stay responsive; SDK serializes via the daemon.
        try:
            mini.play_move(move)
        except Exception as e:
            # Same stale-connection case as _call, but the retry cannot run
            # under _lock: a move blocks for its whole duration and would
            # stall every state read behind it.
            if not _is_disconnect(e):
                raise
            logger.warning("[reachy] play '%s': connection lost — reconnecting", name)
            with self._lock:
                self._reconnect()
                mini = self._mini
            mini.play_move(move)

    def _play_recording(self, name: str) -> None:
        if self._suppressed or self._frozen:
            logger.debug("[reachy] play '%s' blocked (suppressed/frozen)", name)
            return
        with self._lock:
            self._play_gen += 1
            gen = self._play_gen
        if self._play_thread and self._play_thread.is_alive():
            self._cancel_move()

        def _run():
            moves = self._recorded_moves()
            if moves is None:
                logger.warning("[reachy] play '%s' skipped — no move library", name)
                return
            current = name
            try:
                while True:
                    hf_name = _MOVE_MAP.get(current, current)
                    if hf_name != current:
                        logger.debug(
                            "[reachy] mapped recording '%s' → HF move '%s'", current, hf_name
                        )
                    failed = False
                    move = None
                    try:
                        move = moves.get(hf_name)
                    except Exception as e:
                        logger.warning(
                            "[reachy] move '%s' (HF: '%s') unavailable: %s", current, hf_name, e
                        )
                        failed = True
                    if move is None:
                        # No exception but nothing to play — treat as a failure
                        # so the loop can't spin without doing any work.
                        failed = True
                    else:
                        self._current_recording = current
                        try:
                            self._play_move_once(move, current)
                        except Exception as e:
                            logger.warning(
                                "[reachy] play '%s' (HF: '%s') failed: %s", current, hf_name, e
                            )
                            failed = True
                    # Repeat while music plays — the feetech backend does the
                    # same in _continue_playback: a finished recording falls
                    # back to the music groove (and an emotion played mid-track
                    # hands the servo back to it) instead of stopping.
                    next_rec = self._next_groove(current, gen, failed=failed)
                    if next_rec is None:
                        return
                    current = next_rec
            finally:
                # Only the current owner clears it; a stale thread finishing
                # late must not blank the recording a newer play just set.
                if gen == self._play_gen:
                    self._current_recording = None

        self._play_thread = threading.Thread(
            target=_run, daemon=True, name="reachy-play"
        )
        self._play_thread.start()
