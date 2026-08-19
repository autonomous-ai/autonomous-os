import os
import csv
import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set
from hal.follower import LeLampFollowerConfig, LeLampFollower
from hal.presets import EMO_SLEEPY, SERVO_CMD_PLAY, SERVO_CMD_MUSIC_START, SERVO_CMD_MUSIC_STOP, SERVO_IDLE, SERVO_MUSIC_GROOVE

logger = logging.getLogger(__name__)

# Default interpolation duration for move_to (seconds)
DEFAULT_MOVE_DURATION = 2.0

# Zero/hold position in raw encoder units — the physical resting pose after release.
# wrist_pitch (-59.18°, raw 1914) exceeds calibrated range_min=2044, so move_to_raw is used.
ZERO_RAW = {
    "base_yaw":    2100,  #   5.22° — mid=2041.5
    "base_pitch":  2082,  # -20.25° — mid=2312.5
    "elbow_pitch": 2019,  # -30.54° — mid=2366.5
    "wrist_roll":  2070,  #   0.00° — mid=2070.0
    "wrist_pitch": 1914,  # -59.18° — mid=2588.0
}

# Wake/resume position in raw encoder units — all 5 joints.
# Pre-computed from calibration JSON: raw = int(deg * 4095/360 + mid), mid=(range_min+range_max)/2.
# wrist_pitch (-68.48°, raw 1809) exceeds calibrated range_min=2044, so move_to_raw is used.
RESUME_STARTUP_RAW = {
    "base_yaw":    2109,  #   5.96° — mid=2041.5
    "base_pitch":  2105,  # -18.20° — mid=2312.5
    "elbow_pitch": 2233,  # -11.68° — mid=2366.5
    "wrist_roll":  2070,  #   0.00° — mid=2070.0
    "wrist_pitch": 1809,  # -68.48° — mid=2588.0
}

# Duration for the startup/resume move (seconds)
STARTUP_MOVE_DURATION = 5.0

# Peak joint speed the STS3215 can actually deliver, in degrees/second.
# Measured on device: recordings commanding >500 deg/s leave the servo 55 deg
# behind its goal — it saturates, lags, then snaps, which is the audible
# grinding. Recordings are resampled so no segment exceeds this; segments that
# would are stretched in time instead. Set HAL_SERVO_MAX_DPS=0 to disable
# stretching and play recordings at their authored speed.
SERVO_MAX_DPS = float(os.environ.get("HAL_SERVO_MAX_DPS", "250"))

# Recordings are authored at ~20 Hz but the playback loop steps one frame per
# tick at self.fps, so raw frames play at the wrong wall-clock speed. Frames are
# resampled onto the loop's own grid at load time; this is the CSV column that
# carries the authored timing.
RECORDING_TIME_COLUMN = "timestamp"

# Internal event: wake move + state sync, queued by start() so the 5s
# interpolation runs in the event thread instead of blocking the caller
# (server lifespan Phase 3 joins the servo init thread).
SERVO_CMD_STARTUP_MOVE = "__startup_move__"

# Recordings that hold final pose instead of returning to idle
# (e.g. sleepy — lamp stays still until woken by presence/wake-word)
NO_IDLE_RECORDINGS = {EMO_SLEEPY}


def _motor_positions_from_bus(robot: LeLampFollower) -> Dict[str, float]:
    """Read Present_Position only — same numeric scale as CSV, no camera/LED path.

    get_observation() also reads cameras; async_read can block or stall on device.
    If sync_read hangs, the animation thread stops (symptom: HTTP 200 but no motion).
    """
    t0 = time.perf_counter()
    raw = robot.bus.sync_read("Present_Position")
    dt = time.perf_counter() - t0
    if dt > 0.75:
        logger.warning("slow sync_read Present_Position: %.2fs (serial/USB may be stalling)", dt)
    return {f"{motor}.pos": float(val) for motor, val in raw.items()}


class AnimationService:
    def __init__(self, port: str, lamp_id: str, fps: int = 30, duration: float = 5.0, idle_recording: str = SERVO_IDLE, hold_s: float = 0.0):
        self.port = port
        self.lamp_id = lamp_id
        self.fps = fps
        self.duration = duration
        self.idle_recording = idle_recording
        self.hold_s = hold_s
        self._hold_until: float = 0.0  # timestamp until which to hold pose before returning to idle
        self._no_idle_recordings = NO_IDLE_RECORDINGS
        # disable_torque_on_disconnect=False: dropping torque is what `release()`
        # does, deliberately and on request ("arm limp"). A shutdown is not that
        # — it happens on every HAL restart, and cutting torque there would let
        # the arm fall under its own weight for the ~20s until HAL is back.
        # The flag never mattered while nothing called stop(); the shutdown path
        # does now, so make the intent explicit rather than inherit a default
        # that would turn each restart into a release.
        self.robot_config = LeLampFollowerConfig(
            port=port, id=lamp_id, disable_torque_on_disconnect=False
        )
        self.robot: LeLampFollower = None
        self.recordings_dir = os.path.join(os.path.dirname(__file__), "..", "..", "recordings")

        # State management
        self._recording_cache: Dict[str, List[Dict[str, float]]] = {}
        self._current_state: Optional[Dict[str, float]] = None
        self._current_recording: Optional[str] = None
        self._current_frame_index: int = 0
        self._current_actions: List[Dict[str, float]] = []
        self._interpolation_frames: int = 0
        self._interpolation_total_frames: int = 0  # denominator for progress; must match _interpolation_frames initial value
        self._interpolation_target: Optional[Dict[str, float]] = None

        # Music groove: loop while music is playing
        self._music_playing = False
        self._music_recording = SERVO_MUSIC_GROOVE

        # Deterministic stop. Set by halt(), checked every frame by the move and
        # playback loops; they return where they are, leaving the last goal
        # written and torque ON. Cleared by the next commanded motion, so a halt
        # stops what is in flight without wedging the driver.
        self._halt = threading.Event()

        # Custom event handling
        self._running = threading.Event()
        self._event_queue = []
        self._event_lock = threading.Lock()
        self._event_thread: Optional[threading.Thread] = None

        # Serial bus lock — all bus access (read/write/ping) must hold this lock
        self.bus_lock = threading.RLock()

        # One-shot duration override for the next _handle_play interpolation (resume slow-start).
        # Set before dispatch; consumed and cleared inside _handle_play.
        self._resume_duration: Optional[float] = None

        # Freeze flag — when set, _continue_playback() skips servo writes so camera can capture a stable frame
        self._frozen = threading.Event()

        # Last move_to_raw write time (raw register writes bypass
        # robot.send_action, so they need their own stamp — see last_servo_write)
        self._raw_write_monotonic = 0.0

        # Hold mode — suppresses idle/ambient animations but allows emotion dispatch.
        # Set by /servo/hold, cleared by /servo/resume. Also set by scene
        # presets (focus/reading) that want the lamp to stay put while still
        # letting scene-change emotions (greeting/sleepy/stretching) play.
        self._hold_mode = False
        # True only when the hold came from an EXPLICIT /servo/hold (agent
        # command like "face the wall and stay there"). Then even scene-change
        # emotions must not move the servo — a trailing [HW:/emotion:greeting]
        # in the same reply used to play its animation right after aim+hold
        # and park the arm at the greeting pose instead of the commanded one.
        # Scene-preset holds keep the scene-change exemption (greeting/sleepy/
        # stretching legitimately transition a focus/reading scene).
        self._hold_explicit = False

        # Tracking lock — stricter than hold_mode: absolutely no servo writes
        # from the animation loop, and in-progress recordings are dropped so
        # they don't fight the tracker or resume jerking when tracking ends.
        # Set only by the tracker service.
        self._tracking_active = False

        # When True, idle recording finished and pose is held — loop sleeps longer to save CPU
        self._idle_settled = False

    # P gain — match upstream default (16 for all). Higher values cause jerky motion.
    _SERVO_PGAIN = {1: 16, 2: 16, 3: 16, 4: 16, 5: 16}

    def _configure_servos_raw(self):
        """Configure servos directly via scservo_sdk, bypassing lerobot.

        lerobot's bus.write() requires a fully successful connect() handshake.
        When servos are offline, connect() fails and bus.write() raises
        DeviceNotConnectedError. This method writes directly to the serial bus
        to configure whichever servos are actually online.
        """
        with self.bus_lock:
            ph = self.robot.bus.port_handler
            pk = self.robot.bus.packet_handler
            from scservo_sdk import COMM_SUCCESS
            for motor_name, motor_obj in self.robot.bus.motors.items():
                sid = motor_obj.id
                pgain = self._SERVO_PGAIN.get(sid, 32)
                # Ping first
                _, result, _ = pk.ping(ph, sid)
                if result != COMM_SUCCESS:
                    logger.warning(f"{motor_name} (ID {sid}): offline, skipping")
                    continue
                pk.write1ByteTxRx(ph, sid, 40, 0)   # Torque_Enable = 0
                pk.write1ByteTxRx(ph, sid, 33, 0)   # Operating_Mode = position
                pk.write1ByteTxRx(ph, sid, 21, pgain)  # P_Coefficient
                pk.write1ByteTxRx(ph, sid, 23, 0)   # I_Coefficient
                pk.write1ByteTxRx(ph, sid, 22, 32)  # D_Coefficient
                pk.write1ByteTxRx(ph, sid, 40, 1)   # Torque_Enable = 1
                logger.info(f"{motor_name} (ID {sid}): P={pgain}, torque ON")

    def start(self):
        self.robot = LeLampFollower(self.robot_config)
        try:
            self.robot.connect(calibrate=False)
        except Exception as e:
            logger.warning(f"Robot connect (partial): {e}")

        # Configure servos directly — works even if connect() partially failed
        try:
            self._configure_servos_raw()
        except Exception as e:
            logger.warning(f"Raw configure failed: {e}")

        logger.info(f"Animation service connected to {self.port}")

        # Start the event thread first, then queue wake move + idle. The startup
        # move is the first event so any command dispatched during it queues
        # behind — same physical order as the old synchronous version, but
        # start() returns in ~0.3s instead of ~5.5s.
        self._running.set()
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()
        self.dispatch(SERVO_CMD_STARTUP_MOVE, None)

        # Auto-play idle (same as upstream) so lamp moves immediately after boot
        self.dispatch(SERVO_CMD_PLAY, self.idle_recording)

    def stop(self, timeout: float = 5.0):
        # Stop event processing
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=timeout)
        
        if self.robot:
            self.robot.disconnect()
            self.robot = None

    def _sync_state_from_hardware(self) -> None:
        """Set _current_state from Present_Position for all joints.

        Required before interpolating to a recording: partial state with missing keys
        was treated as 0°, causing violent corrections and mechanical jam / overload.
        """
        if not self.robot:
            return
        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
        except Exception as e:
            logger.warning(f"sync state from hardware failed: {e}")
    
    def dispatch(self, event_type: str, payload: Any):
        """Dispatch an event - same interface as ServiceBase"""
        if not self._running.is_set():
            print(f"Animation service is not running, ignoring event {event_type}")
            return
        
        with self._event_lock:
            self._event_queue.append((event_type, payload))
    
    def _event_loop(self):
        """Custom event loop that supports interruption.

        Runs at self.fps throughout. Idle used to step at 5 Hz to save CPU, but
        frames are sampled at self.fps, so stepping slower stretched idle 6x and
        delivered breathing as five visible jerks a second — the reduction cost
        more in smoothness than it saved in CPU.
        """
        while self._running.is_set():
            # Check for events
            with self._event_lock:
                if self._event_queue:
                    event_type, payload = self._event_queue.pop(0)
                else:
                    event_type, payload = None, None

            if event_type:
                self._idle_settled = False
                try:
                    self.handle_event(event_type, payload)
                except Exception as e:
                    print(f"Error handling event {event_type}: {e}")

            # Continue current playback
            self._continue_playback()

            time.sleep(1.0 / self.fps)
    
    def _handle_startup_move(self):
        """Wake move to startup pose + hardware state sync.

        _sync_state_from_hardware must run after the move: the following idle
        interpolation starts from _current_state, and missing/stale joints
        treated as 0° cause violent corrections and servo stall.
        """
        # Move all joints to wake/startup position (includes wrist_pitch outside calib range).
        # should_abort ties the move to _running: a stop/restart of the event
        # loop (aim_servo, shutdown release) interrupts it within one frame, so
        # join() succeeds instead of timing out into a second event thread, and
        # no goal writes land after a torque-off.
        try:
            self.move_to_raw(
                RESUME_STARTUP_RAW,
                duration=STARTUP_MOVE_DURATION,
                should_abort=lambda: not self._running.is_set(),
            )
            logger.info("Servos moved to startup position")
        except Exception as e:
            logger.warning(f"Failed to move to startup position: {e}")
        self._sync_state_from_hardware()

    def handle_event(self, event_type: str, payload: Any):
        if event_type == SERVO_CMD_STARTUP_MOVE:
            self._handle_startup_move()
        elif event_type == SERVO_CMD_PLAY:
            self._handle_play(payload)
        elif event_type == SERVO_CMD_MUSIC_START:
            self._handle_music_start(payload)
        elif event_type == SERVO_CMD_MUSIC_STOP:
            self._handle_music_stop()
        else:
            print(f"Unknown event type: {event_type}")

    def _handle_music_start(self, recording_name: Optional[str] = None):
        """Start grooving to music -- loops recording until music stops.

        recording_name: one of music_groove, music_jazz, music_classical,
                        music_hiphop, music_rock, music_waltz,
                        music_chill, music_hype.
                        Falls back to music_groove when None or unknown.
        """
        self._music_recording = recording_name if recording_name else SERVO_MUSIC_GROOVE
        self._music_playing = True
        self._handle_play(self._music_recording)

    def _handle_music_stop(self):
        """Stop music groove — interrupt immediately and return to idle."""
        self._music_playing = False
        self._hold_until = 0.0  # skip hold, go to idle right away
        self._handle_play(self.idle_recording)
    
    def _handle_play(self, recording_name: str):
        """Start playing a recording with interpolation from current state"""
        self._begin_motion()
        self._idle_settled = False
        self._holding_logged = False
        self._hold_logged = False
        if not self.robot:
            print("Robot not connected")
            return

        # Load the recording
        actions = self._load_recording(recording_name)
        if actions is None:
            return
        
        print(f"Starting {recording_name} with interpolation")
        
        # Set up new playback
        self._current_recording = recording_name
        self._current_actions = actions
        self._current_frame_index = 0
        
        # If we have a current state, set up interpolation to the first frame.
        # _resume_duration overrides self.duration once (set by resume endpoint for slow-start).
        if self._current_state is not None:
            effective_duration = self._resume_duration if self._resume_duration is not None else self.duration
            self._resume_duration = None
            total = int(effective_duration * self.fps)
            self._interpolation_frames = total
            self._interpolation_total_frames = total
            self._interpolation_target = actions[0]
        else:
            self._interpolation_frames = 0
            self._interpolation_target = None
    
    def freeze(self):
        """Pause servo writes so camera can capture a stable frame."""
        self._frozen.set()

    def unfreeze(self):
        """Resume servo writes after camera capture."""
        self._frozen.clear()

    @property
    def is_frozen(self) -> bool:
        """True while a camera consumer wants the servos still. Honored by the
        animation loop AND by the tracker's servo worker (tracker_service)."""
        return self._frozen.is_set()

    # Recordings whose movement is gentle enough to be inaudible on the
    # sensing mic — exempt from is_actively_moving. Idle breathing is exempt
    # implicitly (via _idle_settled); extend this set through
    # HAL_QUIET_RECORDINGS (comma-separated names) as more are measured.
    _QUIET_RECORDINGS: frozenset = frozenset(
        r.strip()
        for r in os.environ.get("HAL_QUIET_RECORDINGS", "").split(",")
        if r.strip()
    )

    @property
    def is_actively_moving(self) -> bool:
        """True while the arm makes AUDIBLE movement: the vision tracker owns
        the servos, or a recording is playing and hasn't settled into the
        idle loop yet (covers emotion/scanning plays and the swing back to
        idle). Two exemptions: idle breathing (settled — writes servo
        positions continuously but is acoustically silent, ~11 RMS measured
        vs 500+ for real animations) and recordings listed in
        HAL_QUIET_RECORDINGS. Used by SoundPerception so the lamp doesn't
        startle at its own joints."""
        if self._tracking_active:
            return True
        rec = self._current_recording
        if rec is None or self._idle_settled:
            return False
        return rec not in self._QUIET_RECORDINGS

    @property
    def last_servo_write(self) -> float:
        """Monotonic timestamp of the last servo motion command, across ALL
        write paths: robot.send_action (animation loop, tracker worker,
        move_to, motors_service) and move_to_raw (direct register writes).
        0.0 when nothing has moved yet. Used by capture_still to wait for a
        frame taken after the arm went quiet."""
        via_action = getattr(self.robot, "last_write_monotonic", 0.0) if self.robot else 0.0
        return max(via_action, self._raw_write_monotonic)

    def _continue_playback(self):
        """Continue current playback - called every frame"""
        if not self._current_recording or not self._current_actions:
            return

        # Skip servo writes while frozen (camera stabilization)
        if self._frozen.is_set():
            return

        # Halt: drop the recording where it is and stop writing. Same shape as
        # the tracking drop below, and deliberately BEFORE it — a halt outranks
        # every other reason to keep playing. No goal is written, so the servos
        # hold the last frame that landed.
        if self._halt.is_set():
            logger.info("[halt] dropped recording %r mid-playback", self._current_recording)
            self._idle_settled = True
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0
            self._interpolation_frames = 0
            self._interpolation_target = None
            return

        # Tracking lock: tracker owns the servo. Drop any in-progress
        # recording (including the interpolation phase before its first
        # frame) so nothing fights the tracker or resumes jerking when
        # tracking ends. This is stricter than hold_mode — /servo/hold
        # and focus scenes still let emotion animations play.
        if self._tracking_active:
            self._idle_settled = True
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0
            self._interpolation_frames = 0
            self._interpolation_target = None
            return

        try:
            # Handle interpolation to first frame
            if self._interpolation_frames > 0 and self._interpolation_target is not None:
                # Calculate interpolation progress — use stored total so the denominator
                # matches the initial _interpolation_frames value, not always self.duration.
                denom = self._interpolation_total_frames if self._interpolation_total_frames > 0 else int(self.duration * self.fps)
                progress = 1.0 - (self._interpolation_frames / denom)
                progress = max(0.0, min(1.0, progress))
                
                # Interpolate between current state and target
                interpolated_action = {}
                for joint in self._interpolation_target.keys():
                    # Default 0 is unsafe if _current_state is incomplete (see _sync_state_from_hardware).
                    current_val = self._current_state.get(joint) if self._current_state else None
                    if current_val is None:
                        logger.warning(
                            "interpolation: joint %s missing from _current_state, using 0 (risk of jam)",
                            joint,
                        )
                        current_val = 0.0
                    target_val = self._interpolation_target[joint]
                    interpolated_action[joint] = current_val + (target_val - current_val) * progress
                
                with self.bus_lock:
                    self.robot.send_action(interpolated_action)
                self._current_state = interpolated_action.copy()
                self._interpolation_frames -= 1
                return

            # Play current frame
            if self._current_frame_index < len(self._current_actions):
                action = self._current_actions[self._current_frame_index]
                with self.bus_lock:
                    self.robot.send_action(action)
                self._current_state = action.copy()
                self._current_frame_index += 1
            else:
                # Recording finished
                if self._music_playing and self._current_recording == self._music_recording:
                    # Loop music groove while music is playing
                    self._current_frame_index = 0
                elif self._current_recording in self._no_idle_recordings:
                    # Hold final pose indefinitely (e.g. sleepy — wake via new play command)
                    if not getattr(self, '_holding_logged', False):
                        logger.info("Holding final pose for '%s' — no idle fallback", self._current_recording)
                        self._holding_logged = True
                    return
                elif self._hold_mode and not self._music_playing:
                    # Hold mode: keep final pose, do not return to idle
                    self._idle_settled = True
                    if not getattr(self, '_hold_logged', False):
                        logger.info("Hold mode: keeping final pose for '%s'", self._current_recording)
                        self._hold_logged = True
                    return
                elif self._current_recording != self.idle_recording:
                    # Hold pose before returning to idle — skip hold when music is playing
                    if not self._music_playing:
                        if self.hold_s > 0 and self._hold_until == 0.0:
                            self._hold_until = time.time() + self.hold_s
                            return
                        if self._hold_until > 0.0:
                            if time.time() < self._hold_until:
                                return  # still holding
                            self._hold_until = 0.0
                    else:
                        self._hold_until = 0.0  # clear any stale hold
                    # Interpolate back to idle (or music groove if music started)
                    if self._music_playing:
                        next_rec = self._music_recording
                    else:
                        next_rec = self.idle_recording
                    next_actions = self._load_recording(next_rec)
                    if next_actions is not None and len(next_actions) > 0:
                        self._current_recording = next_rec
                        self._current_actions = next_actions
                        self._current_frame_index = 0
                        if self._current_state is not None:
                            total = int(self.duration * self.fps)
                            self._interpolation_frames = total
                            self._interpolation_total_frames = total
                            self._interpolation_target = next_actions[0]
                elif self._hold_mode:
                    # Hold mode active while idle finished — hold pose, reduce FPS
                    self._idle_settled = True
                    return
                else:
                    # Loop idle recording at reduced FPS to save CPU
                    self._idle_settled = True
                    self._current_frame_index = 0
                    
        except Exception as e:
            logger.exception("playback error: %s", e)
            # Reset to safe state
            self._current_recording = None
            self._current_actions = []
            self._current_frame_index = 0
    
    def get_available_recordings(self) -> List[str]:
        """Get list of recording names available for this lamp ID"""
        if not os.path.exists(self.recordings_dir):
            return []
        
        recordings = []
        suffix = f".csv"
        
        for filename in os.listdir(self.recordings_dir):
            if filename.endswith(suffix):
                # Remove the lamp_id suffix to get the recording name
                recording_name = filename[:-len(suffix)]
                recordings.append(recording_name)
        
        return sorted(recordings)
    
    def _stretch_timeline(self, times: List[float], frames: List[Dict[str, float]]) -> List[float]:
        """Widen the gaps that demand more joint speed than the servo can deliver.

        Returns a new, still-monotonic time axis. Only over-speed segments grow;
        everything else keeps its authored timing, so a recording slows down
        exactly where it was impossible and nowhere else.
        """
        if SERVO_MAX_DPS <= 0:
            return times

        out = [times[0]]
        for i in range(1, len(frames)):
            authored_dt = max(times[i] - times[i - 1], 1e-3)
            peak_delta = max(
                (abs(frames[i][j] - frames[i - 1][j]) for j in frames[i]),
                default=0.0,
            )
            needed_dt = peak_delta / SERVO_MAX_DPS
            out.append(out[-1] + max(authored_dt, needed_dt))
        return out

    def _resample_recording(
        self, times: List[float], frames: List[Dict[str, float]], name: str
    ) -> List[Dict[str, float]]:
        """Put frames on the playback loop's own 1/fps grid.

        The loop steps exactly one frame per tick, so a list sampled at self.fps
        plays at real time by construction — no timing logic in the hot path.
        """
        stretched = self._stretch_timeline(times, frames)
        duration = stretched[-1] - stretched[0]
        if duration <= 0:
            return frames

        joints = list(frames[0].keys())
        step = 1.0 / self.fps
        total = max(1, int(round(duration / step)))

        out: List[Dict[str, float]] = []
        src = 0
        for k in range(total + 1):
            t = stretched[0] + min(k * step, duration)
            # stretched[] is monotonic and t only advances, so this walk is O(n).
            while src < len(stretched) - 2 and stretched[src + 1] < t:
                src += 1
            span = stretched[src + 1] - stretched[src]
            p = 0.0 if span <= 0 else (t - stretched[src]) / span
            p = max(0.0, min(1.0, p))
            a, b = frames[src], frames[src + 1]
            out.append({j: a[j] + (b[j] - a[j]) * p for j in joints})

        authored = times[-1] - times[0]
        if SERVO_MAX_DPS > 0 and duration > authored * 1.01:
            logger.info(
                "recording %r stretched %.2fs -> %.2fs to stay under %.0f deg/s",
                name, authored, duration, SERVO_MAX_DPS,
            )
        return out

    def _load_recording(self, recording_name: str) -> Optional[List[Dict[str, float]]]:
        """Load a recording from cache or file, resampled for playback.

        Frames are returned on the event loop's 1/fps grid with over-speed
        segments stretched — see _resample_recording. Playback itself stays a
        plain frame-per-tick walk.
        """
        # Check cache first
        if recording_name in self._recording_cache:
            return self._recording_cache[recording_name]

        csv_filename = f"{recording_name}.csv"
        csv_path = os.path.join(self.recordings_dir, csv_filename)

        if not os.path.exists(csv_path):
            logger.warning(f"Recording not found: {csv_path}")
            return None

        try:
            with open(csv_path, 'r') as csvfile:
                csv_reader = csv.DictReader(csvfile)
                actions = []
                times = []
                for row in csv_reader:
                    # Extract action data (exclude timestamp column)
                    action = {key: float(value) for key, value in row.items() if key != RECORDING_TIME_COLUMN}
                    actions.append(action)
                    raw_t = row.get(RECORDING_TIME_COLUMN)
                    times.append(float(raw_t) if raw_t not in (None, "") else None)

            # Without a usable time axis there is nothing to resample against:
            # play the frames as authored rather than invent timing for them.
            if len(actions) < 2 or any(t is None for t in times):
                if actions and any(t is None for t in times):
                    logger.warning(
                        "recording %r has no %s column — playing frames unresampled",
                        recording_name, RECORDING_TIME_COLUMN,
                    )
                self._recording_cache[recording_name] = actions
                return actions

            actions = self._resample_recording(times, actions, recording_name)

            # Cache the recording
            self._recording_cache[recording_name] = actions
            return actions

        except Exception as e:
            logger.error(f"Error loading recording {recording_name}: {e}")
            return None

    # --- Deterministic stop -------------------------------------------------
    #
    # Three different things are called "stop" around here, so to be explicit:
    #   stop()    — service lifecycle: tear the event loop down.
    #   release() — travel to gravity-rest, THEN cut torque. Moves first.
    #   halt()    — this one. Abort what is in flight, hold position, torque ON.
    #
    # The mechanism is the abort check the frame loops already had for shutdown
    # (`not self._running.is_set()`); halt just gives it a second reason to fire.
    # A loop that returns mid-interpolation leaves the last goal it wrote on the
    # servos, so "stop" and "hold" are the same act — nothing extra to command.

    def _motion_aborted(self) -> bool:
        """True when an in-flight move or playback must stop THIS frame."""
        return self._halt.is_set() or not self._running.is_set()

    def _begin_motion(self) -> None:
        """Clear a previous halt so a newly commanded motion can run.

        Called by the commanded-motion entry points, never by the loops
        themselves: a halt has to outlive the move it interrupted, or the very
        next frame would clear it.
        """
        self._halt.clear()

    def halt(self) -> None:
        """Abort any move/recording in flight and hold position. Torque stays ON."""
        self._halt.set()
        # Pin the servos where they are. The aborted loop already left a goal
        # written, but a halt with nothing in flight (the common case — an
        # operator hitting stop on an idle body) must still be a no-op that
        # cannot drift, and re-writing the present position is that no-op.
        try:
            with self.bus_lock:
                current = _motor_positions_from_bus(self.robot) if self.robot else {}
            if current:
                with self.bus_lock:
                    self.robot.send_action(current)
        except Exception as e:
            # Never raise: halt is the one call that must always be answerable.
            logger.warning("[halt] could not pin current position: %s", e)
        logger.info("[halt] motion halted, holding position (torque ON)")

    def move_to(self, target_positions: Dict[str, float], duration: float = DEFAULT_MOVE_DURATION):
        """Smoothly move servos to target positions using software interpolation.

        Instead of sending the target in one shot (which causes jerky instant jumps),
        this method reads the current position and interpolates at self.fps over the
        given duration — the same approach used for animation playback.

        Args:
            target_positions: dict of joint positions, e.g. {"base_yaw.pos": 0.0, ...}
            duration: time in seconds to reach the target (default 2.0)
        """
        if not self.robot:
            raise RuntimeError("Robot not connected")
        self._begin_motion()

        # Read current positions (bus-only; avoid get_observation camera reads)
        try:
            with self.bus_lock:
                current = _motor_positions_from_bus(self.robot)
            if not current:
                raise ValueError("empty Present_Position read")
        except Exception:
            # Fallback: use last known state or jump directly
            if self._current_state:
                current = self._current_state.copy()
            else:
                with self.bus_lock:
                    self.robot.send_action(target_positions)
                return

        total_frames = max(1, int(duration * self.fps))

        for frame in range(1, total_frames + 1):
            if self._motion_aborted():
                logger.info("move_to aborted at frame %d/%d — holding position", frame, total_frames)
                return
            t0 = time.perf_counter()
            progress = frame / total_frames

            interpolated = {}
            for joint, target_val in target_positions.items():
                cur_val = current.get(joint, target_val)
                interpolated[joint] = cur_val + (target_val - cur_val) * progress

            try:
                with self.bus_lock:
                    self.robot.send_action(interpolated)
            except Exception as e:
                logger.warning(f"Interpolated move frame {frame} failed: {e}")
                break

            dt = time.perf_counter() - t0
            sleep_time = (1.0 / self.fps) - dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Send final target exactly
        try:
            with self.bus_lock:
                self.robot.send_action(target_positions)
        except Exception:
            pass

        # Prefer full pose from hardware so other joints are not left stale
        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
                return
        except Exception as e:
            logger.warning(f"move_to: could not read full state after move: {e}")
        self._current_state = target_positions.copy()

    def move_and_hold(self, target_positions: Dict[str, float], duration: float = DEFAULT_MOVE_DURATION):
        """Take over the servo for an explicit /servo/move or /servo/nudge.

        Clearing the active recording makes the animation loop go passive
        (_continue_playback returns early when there is no recording), so a
        concurrently-playing emotion animation STOPS and can no longer overwrite
        the commanded pose frame-by-frame — the race that made `nudge`/`move`
        silently lose to an in-flight emotion. After the move the servo holds the
        commanded pose until the next play/emotion/idle command (or stays held
        when /servo/hold is active).
        """
        # Preempt: drop any recording the event loop is playing so it stops
        # sending its frames. _continue_playback short-circuits on empty state.
        self._current_recording = None
        self._current_actions = []
        self._current_frame_index = 0
        self._interpolation_frames = 0
        self._interpolation_target = None
        self._idle_settled = True

        if duration > 0:
            self.move_to(target_positions, duration=duration)
        else:
            with self.bus_lock:
                self.robot.send_action(target_positions)
            # Keep _current_state in sync so the next play interpolates from here.
            try:
                with self.bus_lock:
                    pos = _motor_positions_from_bus(self.robot)
                self._current_state = pos if pos else dict(target_positions)
            except Exception:
                self._current_state = dict(target_positions)

    def move_to_raw(
        self,
        target_raw: Dict[str, int],
        duration: float = DEFAULT_MOVE_DURATION,
        should_abort: Optional[Callable[[], bool]] = None,
    ):
        """Smoothly move servos to raw encoder positions via direct STS3215 register writes.

        Bypasses lerobot calibration range limits entirely. Use for release/collapse
        positions that exceed the calibrated range_min/max. Caller pre-computes raw
        encoder targets so no bus.calibration access is needed here.

        Args:
            target_raw: motor_name → raw encoder value (0-4095), e.g. {"base_pitch": 1456}
            duration: seconds to reach the target
            should_abort: checked every frame; True stops the move mid-flight.
                The release/park path passes None — parking must always finish.
        """
        if not self.robot:
            raise RuntimeError("Robot not connected")

        GOAL_POSITION_REG = 42     # STS3215: Goal_Position (2 bytes, LSB first)
        PRESENT_POSITION_REG = 56  # STS3215: Present_Position (2 bytes)

        ph = self.robot.bus.port_handler
        pk = self.robot.bus.packet_handler

        # Read current raw positions directly (bypasses normalization)
        current_raw: Dict[str, int] = {}
        with self.bus_lock:
            for motor_name, motor_obj in self.robot.bus.motors.items():
                data, result, _ = pk.read2ByteTxRx(ph, motor_obj.id, PRESENT_POSITION_REG)
                # result==0 means COMM_SUCCESS in scservo_sdk
                current_raw[motor_name] = data if result == 0 else target_raw.get(motor_name, 2048)

        total_frames = max(1, int(duration * self.fps))

        for frame in range(1, total_frames + 1):
            if should_abort and should_abort():
                logger.info("move_to_raw aborted at frame %d/%d", frame, total_frames)
                return
            t0 = time.perf_counter()
            progress = frame / total_frames

            with self.bus_lock:
                for motor_name, target in target_raw.items():
                    cur = current_raw.get(motor_name, target)
                    raw = max(0, min(4095, int(cur + (target - cur) * progress)))
                    motor_obj = self.robot.bus.motors.get(motor_name)
                    if motor_obj:
                        pk.write2ByteTxRx(ph, motor_obj.id, GOAL_POSITION_REG, raw)
            self._raw_write_monotonic = time.monotonic()

            dt = time.perf_counter() - t0
            sleep_time = (1.0 / self.fps) - dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Final exact write
        with self.bus_lock:
            for motor_name, raw in target_raw.items():
                motor_obj = self.robot.bus.motors.get(motor_name)
                if motor_obj:
                    pk.write2ByteTxRx(ph, motor_obj.id, GOAL_POSITION_REG, raw)
        self._raw_write_monotonic = time.monotonic()

        try:
            with self.bus_lock:
                pos = _motor_positions_from_bus(self.robot)
            if pos:
                self._current_state = pos
        except Exception as e:
            logger.warning("move_to_raw: could not read state after move: %s", e)

    # -----------------------------------------------------------------------
    # Public accessors — MotionService contract (hal/drivers/motors/base.py)
    #
    # Routes call these instead of reaching into .robot / .bus / .bus_lock.
    # Keeps all lerobot/scservo internals inside this class.
    # -----------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.robot is not None and getattr(self.robot, "is_connected", False)

    def get_joint_names(self) -> Set[str]:
        """Valid joint keys, e.g. {"base_yaw.pos", "base_pitch.pos", ...}."""
        if not self.robot or not self.robot.bus or not self.robot.bus.motors:
            return set()
        return {f"{m}.pos" for m in self.robot.bus.motors}

    def get_positions(self) -> Dict[str, float]:
        """Read current positions from hardware (bus-only, no camera)."""
        if not self.robot:
            raise RuntimeError("Robot not connected")
        with self.bus_lock:
            obs = self.robot.get_observation()
        return {k: v for k, v in obs.items() if k.endswith(".pos")}

    def send_positions(self, positions: Dict[str, float]) -> None:
        """Write joint positions directly (one-shot, no interpolation)."""
        if not self.robot:
            raise RuntimeError("Robot not connected")
        with self.bus_lock:
            self.robot.send_action(positions)

    @property
    def is_suppressed(self) -> bool:
        """True when zero_pose or explicit hold is active."""
        return getattr(self, "_zero_mode", False) or self._hold_mode

    def ensure_running(self) -> None:
        """Restart the event loop if it stopped (e.g. after zero/hold)."""
        if not self._running.is_set():
            self._running.set()
            self._event_thread = threading.Thread(
                target=self._event_loop, daemon=True,
            )
            self._event_thread.start()
            logger.info("Animation event loop restarted")

    def add_recording(self, name: str, actions: List[Dict[str, float]]) -> None:
        """Invalidate the cache for a recording (used after upload).

        `actions` arrives stripped of its timestamp column, so caching it here
        would store frames that never went through _resample_recording — the
        uploaded copy would play at raw frame rate while the identical file read
        from disk played correctly. The upload route writes the CSV before
        calling this, so dropping the entry lets the normal load path pick it up.
        """
        self._recording_cache.pop(name, None)

    def hold(self, explicit: bool = False) -> None:
        """Suppress idle/ambient animations, torque stays ON."""
        self._hold_mode = True
        if explicit:
            self._hold_explicit = True
        logger.info(
            "Hold mode activated (explicit=%s) — idle suppressed%s",
            explicit, ", emotion servo fully suppressed" if explicit else "",
        )

    def zero_pose(self) -> None:
        """Move to zero/park pose and hold (torque ON). Stops event loop."""
        self._zero_mode = True
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        try:
            self._configure_servos_raw()
        except Exception as e:
            logger.warning("zero: raw configure failed: %s", e)
        try:
            self.move_to_raw(ZERO_RAW, duration=2.0)
        except Exception as e:
            logger.warning("Could not move to zero: %s", e)
        self._sync_state_from_hardware()

    def release(self) -> Dict[str, str]:
        """Move to gravity-rest, then disable torque. Returns per-motor errors."""
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        # Gravity-rest pose in raw encoder units.
        rest_raw = {
            "base_yaw":    2063,
            "base_pitch":  1645,
            "elbow_pitch": 1748,
            "wrist_roll":  2067,
            "wrist_pitch": 2125,
        }
        try:
            self.move_to_raw(rest_raw, duration=2.0)
        except Exception as e:
            logger.warning("Could not move to rest before release: %s", e)
        time.sleep(0.4)
        errors: Dict[str, str] = {}
        if not self.robot or not self.robot.bus:
            return errors
        bus = self.robot.bus
        with self.bus_lock:
            for motor_name in bus.motors:
                try:
                    bus.write("Torque_Enable", motor_name, 0)
                except Exception as e:
                    errors[motor_name] = str(e)
        if errors:
            logger.warning("Servo release errors (offline?): %s", errors)
        else:
            logger.info("release: torque disabled on all servos (arm limp)")
        return errors

    def resume(self) -> None:
        """Exit zero/hold, re-enable torque, restart idle animation."""
        self._zero_mode = False
        self._hold_mode = False
        self._hold_explicit = False
        self._running.clear()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=3.0)
        try:
            self._configure_servos_raw()
        except Exception as e:
            logger.warning("resume: raw configure failed: %s", e)
        self._sync_state_from_hardware()
        self._resume_duration = self.duration
        self._running.set()
        self.dispatch(SERVO_CMD_PLAY, self.idle_recording)
        self._event_thread = threading.Thread(
            target=self._event_loop, daemon=True,
        )
        self._event_thread.start()
        logger.info("Servo resumed from zero-hold mode")

    def joint_status(self) -> Dict[str, dict]:
        """Per-joint online/offline status with angle and servo ID."""
        if not self.robot or not self.robot.bus:
            return {}
        bus = self.robot.bus
        ph = bus.port_handler
        pk = bus.packet_handler
        from scservo_sdk import COMM_SUCCESS

        servos: Dict[str, dict] = {}
        with self.bus_lock:
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
        return servos

    def aim(self, direction: str, duration: float,
            current_positions: Dict[str, float],
            safety_policy: object) -> Dict[str, float]:
        """Aim to a named direction. Returns the final joint positions."""
        from hal.presets import AIM_PRESETS, AIM_LEFT, AIM_RIGHT, AIM_CENTER
        from hal.safety.policy import min_move_duration

        preset = AIM_PRESETS.get(direction)
        if preset is None:
            # Unknown direction (the LLM reached for a word that isn't a preset,
            # e.g. "front") — aim the neutral center pose instead of failing the
            # whole HW node.
            logger.warning("Unknown aim direction %r — defaulting to center", direction)
            direction = AIM_CENTER
            preset = AIM_PRESETS[AIM_CENTER]

        # Left/right only change yaw; other directions set all joints but
        # keep current yaw. This is the lamp's 5-DOF kinematic convention.
        if direction in (AIM_LEFT, AIM_RIGHT):
            positions = {**current_positions, "base_yaw.pos": preset["base_yaw.pos"]}
        else:
            positions = {**preset, "base_yaw.pos": current_positions.get("base_yaw.pos", preset["base_yaw.pos"])}

        eff_duration = min_move_duration(safety_policy, positions, current_positions, duration)

        was_running = self._running.is_set()
        if was_running:
            self._running.clear()
            if self._event_thread and self._event_thread.is_alive():
                self._event_thread.join(timeout=2.0)

        try:
            if eff_duration > 0:
                self.move_to(positions, duration=eff_duration)
            else:
                self.send_positions(positions)
        finally:
            if was_running and not self._running.is_set():
                hold_pos = self._current_state
                if hold_pos:
                    self._current_recording = "__aim_hold__"
                    self._current_actions = [hold_pos]
                    self._current_frame_index = 0
                    self._hold_until = time.time() + 5.0
                self._running.set()
                self._event_thread = threading.Thread(
                    target=self._event_loop, daemon=True,
                )
                self._event_thread.start()
                if not hold_pos:
                    self.dispatch(SERVO_CMD_PLAY, self.idle_recording)

        return positions

    def nudge(self, yaw: float, pitch: float, duration: float,
              current_positions: Dict[str, float],
              safety_policy: object) -> Dict[str, float]:
        """Relative nudge from current position. Returns final positions."""
        from hal.safety.policy import min_move_duration

        positions = dict(current_positions)
        if yaw != 0:
            positions["base_yaw.pos"] = current_positions.get("base_yaw.pos", 0) + yaw
        if pitch != 0:
            positions["base_pitch.pos"] = current_positions.get("base_pitch.pos", 0) + pitch

        eff_duration = min_move_duration(safety_policy, positions, current_positions, duration)
        self.move_and_hold(positions, duration=eff_duration)
        return positions
