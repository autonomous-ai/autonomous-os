"""Tests for the gaze wake trigger.

The angle cases below are built from geometry rather than from captured frames,
so they state what the estimator is supposed to mean rather than re-recording
what it currently outputs.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest

import hal.config as config
from hal.drivers.tracking import detection, gaze


def _landmarks(right_eye, left_eye, nose, mouth_r=(0.0, 0.0), mouth_l=(0.0, 0.0)):
    """Flatten five (x, y) points the way YuNet returns them."""
    return (
        right_eye[0], right_eye[1],
        left_eye[0], left_eye[1],
        nose[0], nose[1],
        mouth_r[0], mouth_r[1],
        mouth_l[0], mouth_l[1],
    )


# --- head_yaw_deg -----------------------------------------------------------



def _fill_dy(dy, from_face=True, n=12, span=None):
    """Seed the pitch window with a steady offset.

    `_maybe_pitch` no longer reads one sample — it takes the median over
    GAZE_PITCH_WINDOW_S so idle's roll sweep (a second AIMING axis on this arm,
    device-proven 2026-08-24) averages out. Tests therefore have to present a
    window, not a scalar.
    """
    span = config.GAZE_PITCH_WINDOW_S if span is None else span
    t0 = gaze.time.monotonic() - span
    for i in range(n):
        gaze.record_dy(dy, from_face, now=t0 + span * i / max(1, n - 1))

def test_nose_centred_between_the_eyes_reads_as_facing_forward():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(0.0, abs=0.01)


def test_nose_at_one_eye_reads_as_full_profile():
    # Offset equals half the inter-ocular distance -> sin(yaw) = 1.
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (140.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(90.0, abs=0.01)


def test_yaw_is_unsigned_so_left_and_right_are_treated_alike():
    right = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (130.0, 120.0)))
    left = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (110.0, 120.0)))
    assert right == pytest.approx(left, abs=0.01)
    assert right > 0


def test_a_rolled_head_is_not_mistaken_for_a_turned_one():
    """A tilted but forward-facing head must still read near zero.

    Measuring the nose offset along the image x-axis instead of along the eye
    line would report this as a large turn, and the gate would refuse to open
    for a user resting their head on one hand.
    """
    angle = math.radians(30.0)
    cx, cy = 120.0, 100.0
    dx, dy = 20.0 * math.cos(angle), 20.0 * math.sin(angle)
    right_eye = (cx - dx, cy - dy)
    left_eye = (cx + dx, cy + dy)
    # Nose sits on the eye midpoint, displaced perpendicular to the eye line.
    nose = (cx + 20.0 * math.sin(angle), cy - 20.0 * math.cos(angle))
    assert gaze.head_yaw_deg(_landmarks(right_eye, left_eye, nose)) == pytest.approx(
        0.0, abs=0.01
    )


def test_yaw_is_independent_of_how_close_the_face_is():
    """Doubling every coordinate is the same head twice as near the camera."""
    near = gaze.head_yaw_deg(_landmarks((200.0, 200.0), (280.0, 200.0), (260.0, 240.0)))
    far = gaze.head_yaw_deg(_landmarks((100.0, 100.0), (140.0, 100.0), (130.0, 120.0)))
    assert near == pytest.approx(far, abs=0.01)


def test_degenerate_or_missing_landmarks_return_none_rather_than_raising():
    assert gaze.head_yaw_deg(()) is None
    assert gaze.head_yaw_deg((1.0, 2.0, 3.0)) is None
    # Both eyes at the same point: nothing to normalise by.
    assert gaze.head_yaw_deg(_landmarks((100.0, 100.0), (100.0, 100.0), (110.0, 120.0))) is None
    assert gaze.head_yaw_deg((float("nan"),) * 10) is None


def test_a_nose_past_its_own_eye_clamps_instead_of_raising():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (400.0, 120.0))
    assert gaze.head_yaw_deg(lm) == pytest.approx(90.0, abs=0.01)


# --- facing_lamp ------------------------------------------------------------


def test_a_face_too_few_pixels_is_rejected_however_well_it_faces_the_lamp():
    """Device probe: background colleagues detect at 8-18 px and yield noise.

    Their landmarks span about three pixels, so the yaw computed from them is
    arithmetic on rounding error and must never vote.
    """
    assert gaze.facing_lamp(0.0, config.GAZE_MIN_FACE_PX - 1) is False
    assert gaze.facing_lamp(0.0, config.GAZE_MIN_FACE_PX + 1) is True
    assert gaze.facing_lamp(0.0, 18) is False   # measured: distant colleague
    assert gaze.facing_lamp(0.0, 78) is True    # measured: the seated user


def test_a_head_turned_past_the_cone_is_rejected_however_near():
    assert gaze.facing_lamp(config.GAZE_MAX_YAW_DEG + 1.0, 120) is False
    assert gaze.facing_lamp(config.GAZE_MAX_YAW_DEG - 1.0, 120) is True


def test_no_face_is_not_facing():
    assert gaze.facing_lamp(None, 120) is False


def test_the_cone_widens_toward_the_frame_edge():
    """The lens is not a pinhole at the edge, so the same head reads wider.

    Device-measured: a user who did not move read [8,9,15,5,12,33,35,28] as
    their face drifted outward; the tail must not be refused for a turn that
    never happened.
    """
    assert gaze.cone_for(0.0) == pytest.approx(config.GAZE_MAX_YAW_DEG)
    assert gaze.cone_for(1.0) == pytest.approx(
        config.GAZE_MAX_YAW_DEG * config.GAZE_EDGE_CONE_SCALE
    )
    assert gaze.cone_for(0.5) > gaze.cone_for(0.0)
    # A centred face gets no extra slack — the compensation is applied only
    # where the distortion actually is.
    assert gaze.facing_lamp(35.0, 120, 0.0) is False
    assert gaze.facing_lamp(35.0, 120, 0.9) is True


def test_edge_slack_never_rescues_a_genuine_profile():
    assert gaze.facing_lamp(90.0, 120, 1.0) is False


# --- which face the gate listens to -----------------------------------------


class _FakeYuNet:
    """Stands in for the loaded YuNet model, returning canned detections."""

    def __init__(self, rows):
        self._rows = np.array(rows, dtype=np.float32) if rows else None

    def setInputSize(self, size):
        pass

    def detect(self, frame):
        return 1, self._rows


def _face_row(x, w, h, nose_x=0.0):
    """One YuNet row: [x, y, w, h, 10 landmark coords, score]."""
    return [float(x), 0.0, float(w), float(h)] + [nose_x] * 10 + [0.9]


def _detect(monkeypatch, rows, frame_w=640):
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    frame = np.zeros((480, frame_w, 3), dtype=np.uint8)
    return detection.detect_face_with_landmarks(frame)


def test_the_face_nearest_the_frame_centre_wins_over_the_larger_one(monkeypatch):
    """A colleague leaning in must not take the gate from the seated user.

    Both clear the size floor, so largest-face would hand the sample to the
    nearer colleague at the edge; the lamp's own aim says the centred face is
    the one it is pointed at.
    """
    big = config.GAZE_MIN_FACE_PX * 2
    small = config.GAZE_MIN_FACE_PX + 2
    colleague = _face_row(x=500, w=big, h=big, nose_x=1.0)
    user = _face_row(x=300, w=small, h=small, nose_x=2.0)
    (fx, _, fw, fh), landmarks = _detect(monkeypatch, [colleague, user])
    assert (fx, fw, fh) == (300, small, small)
    assert landmarks[4] == 2.0


def test_a_single_face_is_picked_exactly_as_before(monkeypatch):
    """The common case must not change: one candidate, both policies agree."""
    h = config.GAZE_MIN_FACE_PX * 2
    (fx, _, fw, fh), _ = _detect(monkeypatch, [_face_row(x=500, w=h, h=h)])
    assert (fx, fw, fh) == (500, h, h)


def test_faces_too_small_to_measure_do_not_win_by_sitting_in_the_centre(monkeypatch):
    """Background colleagues detect at 8-18 px; a centred one must not be
    preferred over the user whose yaw is actually measurable."""
    user_h = config.GAZE_MIN_FACE_PX * 2
    background = _face_row(x=310, w=12, h=12)
    user = _face_row(x=40, w=user_h, h=user_h)
    (fx, _, _, fh), _ = _detect(monkeypatch, [background, user])
    assert (fx, fh) == (40, user_h)


def test_with_nobody_measurable_the_largest_face_still_comes_back(monkeypatch):
    """The caller re-aims vertically off this bbox, and that correction is
    needed exactly when every face is too small — returning None would strand
    a camera pointing too low."""
    (fx, _, _, fh), _ = _detect(monkeypatch, [_face_row(x=10, w=8, h=8),
                                              _face_row(x=600, w=18, h=18)])
    assert (fx, fh) == (600, 18)


def test_no_detections_is_still_none(monkeypatch):
    assert _detect(monkeypatch, []) is None


def test_an_infinite_box_is_dropped_instead_of_crashing_the_detector(monkeypatch):
    """Device-observed: YuNet returned a non-finite bbox and int() raised.

        detection.py:224  x, y, fw, fh = int(best[0]), ...
        OverflowError: cannot convert float infinity to integer

    It killed the tracker's detect thread mid-session, on a face leaving the
    frame (offset past 25% of the frame, bbox_area 1.9%, conf 0.29).
    """
    h = config.GAZE_MIN_FACE_PX * 2
    rows = [_face_row(x=float("inf"), w=h, h=h), _face_row(x=200, w=h, h=h)]
    (fx, _, _, fh), _ = _detect(monkeypatch, rows)
    assert (fx, fh) == (200, h)


def test_an_infinite_box_cannot_hide_the_real_face_behind_it(monkeypatch):
    """Infinity wins any largest-by-area contest, so it must be filtered out
    BEFORE the choice is made, not after."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rows = [_face_row(x=10, w=float("inf"), h=float("inf")),
            _face_row(x=300, w=60, h=60)]
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    assert detection._detect_face_yunet(frame) == (300, 0, 60, 60)


def test_every_box_unusable_reads_as_no_face(monkeypatch):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rows = [_face_row(x=float("nan"), w=60, h=60)]
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYuNet(rows))
    assert detection._detect_face_yunet(frame) is None
    assert _detect(monkeypatch, rows) is None


# --- the rolling buffer -----------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_buffer():
    gaze.reset_for_test()
    yield
    gaze.reset_for_test()


def _fill(now, entries):
    """entries: list of (seconds_before_now, yaw_or_None)."""
    for ago, yaw in entries:
        gaze.record_sample(yaw, 80, 0.0, now=now - ago)


def _trail(now, yaws, interval=0.33):
    """Replay a device `trail=[...]` as samples ending at ``now``.

    `None` is a frame where no face was found, printed as `-` in the log.
    """
    last = len(yaws) - 1
    for i, yaw in enumerate(yaws):
        gaze.record_sample(yaw, 80, 0.0, now=now - (last - i) * interval)


def test_a_majority_facing_window_survives_noisy_samples():
    """Isolated wild readings must not veto an otherwise facing window.

    Per-sample yaw carries real measurement noise — device trails show swings
    of 35 degrees between samples a third of a second apart, which no head
    performs. A rule requiring every sample to pass would reject this.
    """
    now = 1000.0
    _trail(now, [10, 15, 8, 90, 12, None, 9, 14])
    ratio, n = gaze.facing_ratio(now)
    assert n >= config.GAZE_MIN_SAMPLES
    assert ratio >= config.GAZE_MIN_FACING_RATIO


def test_a_window_of_a_head_turned_away_does_not_pass():
    """Shape of a real device trail while talking to someone else."""
    now = 1000.0
    _trail(now, [3, 90, 90, 90, 90, 90, 90, 90])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_a_window_caught_mid_turn_does_not_pass_yet():
    """Half the window still looking away is not yet addressing the lamp."""
    now = 1000.0
    _trail(now, [None, 63, 12, 42, 29, 64, 39, 11])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_only_the_window_is_examined_not_the_whole_buffer():
    """Facing a while ago, turned away since -> not facing."""
    now = 1000.0
    _trail(now, [5, 5, 5, 5, 90, 90, 90, 90])
    ratio, _ = gaze.facing_ratio(now)
    assert ratio < config.GAZE_MIN_FACING_RATIO


def test_samples_that_all_predate_the_window_are_not_consulted():
    """Looking over, then away, then speaking is not addressing the lamp."""
    now = 1000.0
    stale = config.GAZE_WINDOW_S + 1.0
    gaze.record_sample(5.0, 80, 0.0, now=now - stale - 0.3)
    gaze.record_sample(5.0, 80, 0.0, now=now - stale)
    ratio, n = gaze.facing_ratio(now)
    assert (ratio, n) == (0.0, 0)


def test_too_few_samples_is_not_evidence_either_way():
    """After start-up, or after a live look monopolised the detector."""
    now = 1000.0
    _trail(now, [5])
    _, n = gaze.facing_ratio(now)
    assert n < config.GAZE_MIN_SAMPLES


def test_an_empty_buffer_yields_no_evidence():
    assert gaze.facing_ratio(1000.0) == (0.0, 0)


def test_samples_older_than_the_buffer_window_are_dropped():
    now = 1000.0
    gaze.record_sample(5.0, 80, 0.0, now=now - (config.GAZE_BUFFER_S + 5.0))
    gaze.record_sample(5.0, 80, 0.0, now=now)
    assert len(gaze.snapshot()) == 1


# --- the decision -----------------------------------------------------------


class _Voice:
    def __init__(self):
        self.grants = []
        self.windows = []

    def grant_wakeword_focus(self, source="button", timeout_s=None):
        self.grants.append(source)
        self.windows.append(timeout_s)
        return True


@pytest.fixture
def voice(monkeypatch):
    import hal.app_state as state

    v = _Voice()
    monkeypatch.setattr(state, "voice_service", v, raising=False)
    return v


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", False)


def _hold_now():
    """Put a facing window in the buffer that ends at the current instant."""
    now = gaze.time.monotonic()
    step = config.GAZE_WINDOW_S / 4.0
    for i in range(config.GAZE_MIN_SAMPLES + 1):
        gaze.record_sample(5.0, 80, 0.0, now=now - i * step)


def test_shadow_mode_logs_but_never_opens_the_gate(monkeypatch, voice, caplog):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_WAKE_SHADOW", True)
    _hold_now()
    with caplog.at_level("INFO"):
        assert gaze.on_speech_start() is False
    assert voice.grants == []
    assert "WOULD_WAKE" in caplog.text


def test_an_armed_hold_opens_the_gate_through_the_same_seam_as_the_button(armed, voice):
    _hold_now()
    assert gaze.on_speech_start() is True
    assert voice.grants == ["gaze"]


def test_a_glance_too_brief_to_be_addressing_does_not_open_the_gate(armed, voice):
    now = gaze.time.monotonic()
    gaze.record_sample(5.0, 80, 0.0, now=now)
    assert gaze.on_speech_start() is False
    assert voice.grants == []


def test_missing_face_requests_reacquire_and_can_authorize_the_same_utterance(armed, voice):
    """A lamp that lost framing must not make the user repeat their sentence."""
    assert gaze.on_speech_start() is False
    assert gaze._speech_repoint_requested.is_set()

    _hold_now()  # samples gathered after the watcher restored the remembered pose
    assert gaze.on_speech_end() is True
    assert voice.grants == ["gaze-reacquired"]
    assert not gaze._speech_repoint_requested.is_set()


def test_a_measured_away_head_never_requests_the_reacquire_exception(armed, voice):
    now = gaze.time.monotonic()
    _trail(now, [90.0, 90.0, 90.0, 90.0])
    assert gaze.on_speech_start() is False
    assert not gaze._speech_repoint_requested.is_set()

    _hold_now()
    assert gaze.on_speech_end() is False
    assert voice.grants == []


def test_voice_rechecks_reacquired_gaze_before_realtime_can_be_authorized():
    """The recovery must still apply to the utterance that caused the turn."""
    voice_source = (
        Path(__file__).parents[1] / "drivers" / "voice" / "voice_service.py"
    ).read_text()
    assert voice_source.index("gaze.on_speech_end()") < voice_source.index(
        "# Noise guard: a session can open"
    )


def test_the_cooldown_stops_one_conversation_opening_a_gate_per_sentence(armed, voice):
    _hold_now()
    assert gaze.on_speech_start() is True
    _hold_now()
    assert gaze.on_speech_start() is False
    assert voice.grants == ["gaze"]


def test_disabled_is_inert_even_with_a_perfect_hold(voice, monkeypatch):
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", False)
    _hold_now()
    assert gaze.on_speech_start() is False
    assert voice.grants == []


def test_a_missing_voice_service_degrades_quietly(armed, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "voice_service", None, raising=False)
    _hold_now()
    assert gaze.on_speech_start() is False


def test_the_watcher_does_not_start_when_there_is_no_gate_to_open(monkeypatch, caplog):
    """Wake word off means every utterance already dispatches."""
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "WAKEWORD_ENABLED", False)
    with caplog.at_level("INFO"):
        gaze.start()
    assert gaze._thread is None or not gaze._thread.is_alive()
    assert "nothing to gate" in caplog.text


# --- turning back toward the remembered bearing -----------------------------


class _Svc:
    """Animation service stand-in that records what it was asked to move to."""

    def __init__(self):
        self.moves = []
        self._tracking_active = False
        self._music_playing = False

    def get_positions(self):
        return {"base_yaw.pos": 40.0, "base_pitch.pos": 0.0}

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos"]

    def move_and_hold(self, target, duration=None):
        self.moves.append(target)


class _Est:
    bearing_deg = 4.0
    confidence = 0.9
    pose = {"base_yaw.pos": 4.0, "base_pitch.pos": 0.0}


@pytest.fixture
def body(monkeypatch):
    import hal.app_state as state
    from hal.drivers.tracking import user_bearing

    svc = _Svc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _Est())
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_REPOINT_ENABLED", True)
    return svc


def _absent_for(seconds):
    """Pretend the last face was seen `seconds` ago."""
    gaze._last_face_t = gaze.time.monotonic() - seconds
    gaze._last_repoint_t = gaze.time.monotonic() - 10_000.0


def test_a_long_absence_turns_the_lamp_to_the_remembered_bearing(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves and body.moves[0]["base_yaw.pos"] == pytest.approx(4.0)


def test_a_brief_absence_does_not_send_the_head_hunting(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S - 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_speech_reacquire_bypasses_the_background_absence_delay(body):
    _absent_for(0.0)
    assert gaze._maybe_repoint(gaze.time.monotonic(), force=True) is True
    assert body.moves and body.moves[0]["base_yaw.pos"] == pytest.approx(4.0)


def test_it_turns_at_most_once_per_cooldown(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    now = gaze.time.monotonic()
    gaze._maybe_repoint(now)
    gaze._maybe_repoint(now + 1.0)
    assert len(body.moves) == 1


def test_it_never_moves_a_body_something_else_owns(body):
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    body._tracking_active = True
    gaze._maybe_repoint(gaze.time.monotonic())
    body._tracking_active = False
    body._music_playing = True
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_bearing_it_does_not_trust_is_not_worth_turning_for(body, monkeypatch):
    from hal.drivers.tracking import user_bearing

    class _Weak(_Est):
        confidence = config.GAZE_REPOINT_MIN_CONFIDENCE - 0.01

    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _Weak())
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_repoint_can_be_switched_off_without_disabling_the_gate(body, monkeypatch):
    monkeypatch.setattr(config, "GAZE_REPOINT_ENABLED", False)
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves == []


def test_a_face_at_the_frame_edge_does_not_count_as_still_being_seen(body, monkeypatch):
    """Edge sightings must not hold off a re-point.

    Device-measured: the user drifted to edge=0.71-0.75 while the idle loop
    walked the camera away, and every one of those sightings reset the absence
    clock, so the lamp never turned to keep them in view.
    """
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze.record_sample(5.0, 120, 0.9)          # big face, but at the edge
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves, "an edge sighting should not postpone the re-point"


def test_frames_where_no_face_was_measured_do_not_vote_against():
    """A dropped frame is no evidence, not evidence of looking away.

    Device-measured: a user sitting still with a 93 px well-centred face
    produced [32,11,-,-,-,34,38,24] and scored 50% against a 60% bar, refused
    for three frames the detector dropped rather than for anything they did.
    """
    now = 1000.0
    _trail(now, [5, 5, None, None, None, 5, 90, 5])
    ratio, n = gaze.facing_ratio(now)
    # The frames that saw nothing are absent from the tally entirely, so the
    # score reflects only what was actually observed.
    assert n == 3
    assert ratio == pytest.approx(2.0 / 3.0)


def test_seeing_nobody_at_all_declines_to_decide_rather_than_passing():
    """The empty denominator must not become a free pass."""
    now = 1000.0
    _trail(now, [None, None, None, None, None, None])
    ratio, n = gaze.facing_ratio(now)
    assert (ratio, n) == (0.0, 0)


def test_faces_too_small_to_measure_are_left_out_of_the_denominator(monkeypatch):
    """Background colleagues neither vote for nor against."""
    now = 1000.0
    step = 0.3
    gaze.record_sample(5.0, 120, 0.1, now=now - 2 * step)   # the user, facing
    gaze.record_sample(5.0, 10, 0.1, now=now - step)        # a distant face
    gaze.record_sample(5.0, 10, 0.1, now=now)               # another
    ratio, n = gaze.facing_ratio(now)
    assert n == 1 and ratio == pytest.approx(1.0)


# --- sampling while the body is busy -----------------------------------------


class _MovingSvc:
    """Animation service whose head last moved `ago` seconds ago."""

    def __init__(self, ago, tracking=False):
        self._ago = ago
        self._tracking_active = tracking

    @property
    def last_servo_write(self):
        return gaze.time.monotonic() - self._ago


def _sample_reason(svc, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    from hal.drivers.tracking import aim

    # Hold the detector lock so the call returns before touching hardware; what
    # matters here is only whether it got that far.
    aim._detector_lock_use.acquire()
    try:
        return gaze._sample_once()
    finally:
        aim._detector_lock_use.release()


def test_a_tracking_session_no_longer_blocks_sampling(monkeypatch):
    """Tracking is the lamp FOLLOWING the user's face.

    It is the state where the lamp is most obviously attending to them, so
    refusing to notice they are addressing it reads as broken. The flag stays
    set for a whole session while the head is mostly still.
    """
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S + 1.0, tracking=True)
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_a_head_still_settling_from_a_move_is_not_sampled(monkeypatch):
    """Mid-swing the yaw describes the lamp's motion, not the user's intent."""
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


class _BreathingSvc(_MovingSvc):
    """The arm looping the idle recording at reduced FPS — writing, not moving."""

    idle_recording = "idle"

    def __init__(self, ago, settled=True, recording="idle"):
        super().__init__(ago)
        self._idle_settled = settled
        self._current_recording = recording


def test_the_idle_loop_breathing_does_not_count_as_a_move(monkeypatch):
    """Idle writes the servos every frame, forever.

    So `last_servo_write` is almost never stale and the settling test alone
    refused nearly every frame: 0.3 samples/s recorded against 4.9/s blocked on
    the device. Idle motion is millimetres and slow; the yaw survives it.
    """
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert gaze.idle_breathing(svc) is True
    # It gets past the settling test now — the next gate is the detector lock,
    # which _sample_reason holds.
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_interpolating_into_idle_is_still_a_real_move(monkeypatch):
    """Unsettled idle is the relocation INTO idle, which does smear the yaw."""
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0, settled=False)
    assert gaze.idle_breathing(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_another_recording_playing_is_not_breathing(monkeypatch):
    from hal.drivers.tracking import aim

    svc = _BreathingSvc(ago=aim.FRAME_SETTLE_S / 2.0, recording="greeting")
    assert gaze.idle_breathing(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_a_service_that_knows_nothing_of_idle_is_treated_as_moving():
    assert gaze.idle_breathing(None) is False
    assert gaze.idle_breathing(object()) is False


def test_a_tracking_pursuit_does_not_count_as_a_move_either(monkeypatch):
    """Tracking writes the arm every frame, so settling was never stale.

    That put the whole of tracking back behind the settling test, through the
    back door, after that test was written specifically not to use
    `_tracking_active` — measured with tracking up: 0.7 samples/s against
    4.5/s blocked, and a user at yaw 0.9 deg with a 130 px face dead centre
    refused for having one sample in the window instead of two.
    """
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0, tracking=True)
    assert gaze.following_a_face(svc) is True
    assert _sample_reason(svc, monkeypatch) == "detector busy with a live look"


def test_a_body_doing_neither_is_still_treated_as_moving(monkeypatch):
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0, tracking=False)
    assert gaze.following_a_face(svc) is False
    assert _sample_reason(svc, monkeypatch) == "head still settling from a move"


def test_a_blocked_turn_is_reported_as_blocked_not_as_a_sample(monkeypatch):
    """The loop's rate figure must count evidence, not attempts.

    Counting iterations reported 5.7/s on the device while the buffer held
    nothing newer than the 1.5 s window — under 1/s of real evidence. Every
    turn that returns a reason recorded nothing, so it belongs in the blocked
    tally, and the two must not be the same number.
    """
    from hal.drivers.tracking import aim

    svc = _MovingSvc(ago=aim.FRAME_SETTLE_S / 2.0)
    assert _sample_reason(svc, monkeypatch) is not None


# --- landmarks that were never actually seen ---------------------------------


def test_landmarks_inside_the_frame_are_measurable():
    lm = _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 120.0))
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is True


def test_an_eye_above_the_top_edge_was_never_seen():
    """Device-measured: box [264, -1, 162, 92], eyes at y=-3.0 and y=-1.3.

    The user was sitting straight in front of the lamp; the camera was aimed
    too low, so the top of the head fell outside the frame and YuNet
    extrapolated the eyes above it.
    """
    lm = _landmarks((336.5, -3.0), (391.0, -1.3), (374.6, 14.5))
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is False


def test_a_landmark_past_any_other_edge_is_refused_too():
    for lm in (
        _landmarks((-2.0, 100.0), (140.0, 100.0), (120.0, 120.0)),
        _landmarks((100.0, 100.0), (700.0, 100.0), (120.0, 120.0)),
        _landmarks((100.0, 100.0), (140.0, 100.0), (120.0, 400.0)),
    ):
        assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is False


def test_a_clipped_mouth_does_not_disqualify_the_yaw():
    """Only the eyes and the nose feed the angle; the mouth is along for the ride."""
    lm = _landmarks(
        (100.0, 100.0), (140.0, 100.0), (120.0, 120.0),
        mouth_r=(105.0, 400.0), mouth_l=(135.0, 400.0),
    )
    assert gaze.landmarks_in_frame(lm, 640.0, 360.0) is True


def test_missing_or_non_finite_landmarks_are_not_in_frame():
    assert gaze.landmarks_in_frame((), 640.0, 360.0) is False
    assert gaze.landmarks_in_frame((1.0, 2.0), 640.0, 360.0) is False
    assert gaze.landmarks_in_frame((float("nan"),) * 10, 640.0, 360.0) is False


def test_a_clipped_face_is_recorded_as_unmeasured_not_as_a_profile(monkeypatch):
    """The whole bug, end to end.

    A face clipped at the top used to record 90.0 — the clamp's output, not a
    measurement — which facing_ratio counted as a vote AGAINST facing. So a
    user looking straight at the lamp produced trail=[90,90,90,90] and was
    refused. It must land in the buffer as "no measurement" instead.
    """
    import hal.app_state as state

    from hal.drivers.tracking import aim, detection as det, frame_utils

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(state, "camera_capture", object(), raising=False)
    monkeypatch.setattr(state, "animation_service", _Svc(), raising=False)
    monkeypatch.setattr(state, "_camera_disabled", False, raising=False)
    monkeypatch.setattr(aim, "_grab_frame", lambda cap, svc: frame)
    monkeypatch.setattr(aim, "get_detector", lambda: None)
    monkeypatch.setattr(frame_utils, "downscale", lambda f: (f, 1.0))
    # The measured detection: eyes above the top edge, face big and centred.
    monkeypatch.setattr(
        det, "detect_face_with_landmarks",
        lambda f: ((264, 0, 162, 92),
                   _landmarks((336.5, -3.0), (391.0, -1.3), (374.6, 14.5))),
    )

    gaze.reset_for_test()
    assert gaze._sample_once() is None
    (_, yaw, px, _), = gaze.snapshot()
    assert yaw == float("inf")     # unmeasured, so it votes neither way
    assert px == pytest.approx(92.0)
    # The frame still says which way to move: the head is above centre.
    assert gaze._last_dy_frac is not None and gaze._last_dy_frac < 0
    assert gaze._last_dy_from_face is True


# --- vertical centring (the neck) -------------------------------------------


class _PitchSvc(_Svc):
    # The correction is spread over all three pitch joints, so a stand-in that
    # only reports the wrist would have every joint start from a default 0.0 and
    # make the assertions below meaningless.
    _ATTR = {
        "base_pitch.pos": "base",
        "elbow_pitch.pos": "elbow",
        "wrist_pitch.pos": "wrist",
    }

    def __init__(self, wrist=-70.0, base=10.0, elbow=5.0):
        super().__init__()
        self.wrist = wrist
        self.base = base
        self.elbow = elbow
        # joint -> (lo, hi) it refuses to move past, the way a stalled servo
        # does. Empty means every joint arrives where it is sent.
        self.stalls = {}

    def get_positions(self):
        return {
            "base_yaw.pos": 0.0,
            "base_pitch.pos": self.base,
            "elbow_pitch.pos": self.elbow,
            "wrist_pitch.pos": self.wrist,
        }

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"]

    def move_and_hold(self, target, duration=None):
        """Record the command AND arrive at it.

        A stand-in that records the move but leaves get_positions() unchanged
        looks exactly like an arm that stalled, so the landing check would treat
        every test as a failed correction and every assertion below would pass
        for the wrong reason.
        """
        self.moves.append(target)
        for joint, want in target.items():
            attr = self._ATTR.get(joint)
            if attr is None:
                continue
            lo, hi = self.stalls.get(joint, (float("-inf"), float("inf")))
            setattr(self, attr, max(lo, min(hi, float(want))))


@pytest.fixture
def neck(monkeypatch):
    import hal.app_state as state

    svc = _PitchSvc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_PITCH_ENABLED", True)
    # The settle before reading the pose back is there for a real bus; paying
    # it on every correction would add seconds to the suite for nothing.
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 0.0)
    gaze._pitch_stalls.clear()
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    # Corrections are face-driven unless a test says otherwise: the guessed
    # path is off by default and has to be opted into explicitly.
    gaze._last_dy_from_face = True
    return svc


def test_a_face_above_centre_tilts_the_camera_up(neck):
    """The case that made the lamp stare at the keyboard all afternoon.

    Up is the INCREASING direction on elbow_pitch and the decreasing direction
    on base_pitch — device-measured 2026-08-25 with base and wrist pinned and
    only elbow moving: +1.6 framed the desk, +54.8 framed the ceiling.
    Asserting the joint numbers rather than the word "up" is the whole point:
    the sign is the thing that was wrong.
    """
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "a clipped-high face should raise the camera"
    assert neck.moves[0]["elbow_pitch.pos"] > 5.0
    assert neck.moves[0]["base_pitch.pos"] < 10.0


def test_a_face_below_centre_tilts_the_camera_down(neck):
    _fill_dy(0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves[0]["elbow_pitch.pos"] < 5.0
    assert neck.moves[0]["base_pitch.pos"] > 10.0


def test_the_lift_does_not_lean_on_the_joint_that_cannot_lift(neck):
    """wrist_pitch is why this feature did nothing for an afternoon.

    Looking up drives wrist NEGATIVE, and on lamp-ac82 it stalls at -34.8 while
    idle rests it near -32 — about 2 deg of headroom. The old loop spent the
    entire correction there: `/servo/move` accepted the target unclamped,
    reported `position error 14.6 deg (target=-49.0, actual=-34.4)`, and the
    head never moved. Whatever the weights become, the upward correction must
    not depend on that joint.
    """
    _fill_dy(-0.9)
    gaze._maybe_pitch(gaze.time.monotonic())
    moved = neck.moves[0]
    wrist_delta = abs(moved["wrist_pitch.pos"] - (-70.0))
    lifting = abs(moved["elbow_pitch.pos"] - 5.0) + abs(moved["base_pitch.pos"] - 10.0)
    assert lifting > wrist_delta, (
        "the wrist cannot deliver an upward correction on this arm; the lift has "
        "to come from base_pitch and elbow_pitch"
    )


def test_the_pitch_sign_is_only_valid_while_the_joint_direction_is():
    """Fail loudly if the calibration stops supporting the sign above.

    The two tests before this one assert a NUMBER — that up is the decreasing
    direction on wrist_pitch. That is not a property of the code, it is a
    property of the arm, established by a paired A/B on the device. A
    recalibration can invert it, and if it does, those tests keep passing while
    `_maybe_pitch` drives the error instead of closing it. That already
    happened once: the A/B predates `6f0c4ec4` and the loop has been off on
    lamp ever since.

    A test cannot measure an arm. It can check the two things the sign depends
    on, which lerobot makes explicit in `motors_bus._normalize`:

        norm = (((bounded_val - min_) / (max_ - min_)) * 200) - 100
        normalized_values[id_] = -norm if drive_mode else norm

    That is the RANGE_M100_100 branch, which is the one in play — `use_degrees`
    is False in config_hal_follower and never overridden (see also the note at
    presets.py:221). So the sign inverts if `drive_mode` becomes non-zero, or if
    the range is stored descending and flips the denominator. `homing_offset`
    shifts the zero without inverting anything, so it is deliberately not
    checked here — it moves poses, not directions.

    Read from whatever calibration the arm actually loaded: the per-unit file
    on a provisioned device, the repo hal.json on a fresh one.
    """
    from hal.drivers.tracking import user_bearing

    path = user_bearing._calibration_path()
    assert path, "no calibration resolved — the pitch sign cannot be vouched for"
    with open(path, encoding="utf-8") as f:
        cal = json.load(f)

    # The joints the correction is actually spread over. wrist_pitch used to be
    # checked here and no longer is — it carries weight 0.0, so its calibration
    # can invert without changing where the camera ends up pointing.
    for joint in ("base_pitch", "elbow_pitch"):
        wp = cal[joint]
        assert wp["drive_mode"] == 0, (
            f"{path}: {joint} drive_mode is {wp['drive_mode']}, not 0. lerobot "
            "negates the normalised value when this is set, so UP is no longer "
            "the direction _maybe_pitch assumes. Redo the A/B before trusting it."
        )
        assert wp["range_min"] < wp["range_max"], (
            f"{path}: {joint} range is stored descending "
            f"({wp['range_min']} -> {wp['range_max']}), which flips the sign of "
            "the normalisation. Redo the A/B before trusting _maybe_pitch."
        )


def test_a_face_near_enough_to_centre_is_left_alone(neck):
    """Every correction is a visible head movement nobody asked for."""
    _fill_dy(config.GAZE_PITCH_DEAD_ZONE_FRAC - 0.01)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_no_face_measured_means_no_correction(neck):
    # empty window: nothing measured at all
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_one_correction_is_bounded(neck):
    """A wrong sign must be a small mistake the next look reverses.

    The bound is on the CAMERA rotation, not on any single joint. The three
    pitch axes are parallel, so the total tilt is the sum of what each joint
    contributed — which is also why the weights sum to 1.0.
    """
    _fill_dy(-1.0)
    gaze._maybe_pitch(gaze.time.monotonic())
    moved = neck.moves[0]
    before = {"base_pitch.pos": 10.0, "elbow_pitch.pos": 5.0, "wrist_pitch.pos": -70.0}
    # elbow is mounted reversed, so its joint delta counts against the camera.
    tilt = (
        (moved["base_pitch.pos"] - before["base_pitch.pos"])
        + (moved["wrist_pitch.pos"] - before["wrist_pitch.pos"])
        - (moved["elbow_pitch.pos"] - before["elbow_pitch.pos"])
    )
    assert tilt == pytest.approx(-config.GAZE_PITCH_MAX_STEP_DEG)


def test_the_correction_stays_inside_the_mechanical_range(neck):
    from hal.drivers.tracking import constants as C

    neck.wrist = C.WRIST_PITCH_MIN + 1.0
    _fill_dy(-1.0)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves[0]["wrist_pitch.pos"] >= C.WRIST_PITCH_MIN


def test_it_never_moves_a_body_something_else_owns(neck):
    _fill_dy(-0.4)
    neck._tracking_active = True
    gaze._maybe_pitch(gaze.time.monotonic())
    neck._tracking_active = False
    neck._music_playing = True
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_corrections_are_rate_limited(neck):
    _fill_dy(-0.4)
    now = gaze.time.monotonic()
    gaze._maybe_pitch(now)
    gaze._maybe_pitch(now + 0.5)
    assert len(neck.moves) == 1


# --- headroom from a person box when no face is measurable -------------------


class _Box:
    """Detector stand-in returning one fixed box for 'person'."""

    def __init__(self, box):
        self.box = box

    def detect(self, frame, target, strict=False, min_conf=None):
        return self.box if target == "person" else None


class _Frame:
    def __init__(self, h=360, w=640):
        self.shape = (h, w, 3)


def test_a_torso_cut_off_at_the_top_asks_the_camera_to_tilt_up():
    """The head is outside the frame, above — the only readable evidence left.

    Without this the correction dead-ends: undoing a large offset takes several
    bounded steps, and the first can push a barely-visible face out of view, at
    which point nothing is measurable and the camera stays wrong forever.
    """
    det = _Box((100, 0, 200, 300))       # top edge, tall
    assert gaze._headroom_from_person(_Frame(), det) == pytest.approx(-0.5)


def test_a_whole_person_well_inside_the_frame_needs_no_correction():
    det = _Box((100, 40, 200, 250))      # top edge clear
    assert gaze._headroom_from_person(_Frame(), det) is None


def test_a_distant_person_is_not_this_desk_and_is_ignored():
    det = _Box((100, 0, 20, 20))         # touching the top, but tiny
    assert gaze._headroom_from_person(_Frame(), det) is None


def test_no_person_and_no_detector_yield_no_correction():
    assert gaze._headroom_from_person(_Frame(), _Box(None)) is None
    assert gaze._headroom_from_person(_Frame(), None) is None
    assert gaze._headroom_from_person(None, _Box((0, 0, 100, 300))) is None


def test_a_detector_that_raises_does_not_break_sampling():
    class _Angry:
        def detect(self, *a, **k):
            raise RuntimeError("model gone")

    assert gaze._headroom_from_person(_Frame(), _Angry()) is None


def test_a_person_filling_the_frame_still_means_look_up():
    """A user sitting close fills the frame top to bottom at every pitch.

    This is the exact case the fallback exists for — a torso with no face above
    it means the camera is too low, because heads sit above bodies and this
    lamp sits below head height. The danger was never the inference, it was
    that it stays true however far the neck has already travelled; the budget
    of blind steps in the caller is what makes acting on it terminate.
    """
    det = _Box((100, 0, 200, 360))       # clipped top AND bottom
    assert gaze._headroom_from_person(_Frame(), det) == pytest.approx(-0.5)


def test_blind_corrections_stop_after_a_few_and_wait_for_a_real_face(neck, monkeypatch):
    """The fallback knows the head is up there, never how far."""
    monkeypatch.setattr(config, "GAZE_PITCH_MAX_BLIND_STEPS", 3)
    gaze._blind_pitch_steps = 0
    for i in range(config.GAZE_PITCH_MAX_BLIND_STEPS + 3):
        # Refill each round: a correction clears the window it was computed
        # from, so on the device the sampler has to rebuild one before the loop
        # may act again. That refill IS the spacing between blind steps now.
        _fill_dy(-0.5, from_face=False)
        gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
        gaze._maybe_pitch(gaze.time.monotonic())
    assert len(neck.moves) == config.GAZE_PITCH_MAX_BLIND_STEPS


def test_the_blind_search_is_off_by_default(neck):
    """It cannot converge — the fallback reports the same offset every time, so
    with the anchor following each step it becomes a one-way ratchet. Device-
    observed -45 -> -30 -> -15 -> 0 -> +14, heading for the ceiling."""
    assert config.GAZE_PITCH_MAX_BLIND_STEPS == 0
    gaze._blind_pitch_steps = 0
    _fill_dy(-0.5, from_face=False)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_a_real_face_clears_the_blind_budget(neck):
    """Seeing a face again means the guessing worked; allow guessing later."""
    gaze._blind_pitch_steps = config.GAZE_PITCH_MAX_BLIND_STEPS
    _fill_dy(-0.4, from_face=True)
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves and gaze._blind_pitch_steps == 0


def test_moving_the_neck_discards_what_was_seen_from_the_old_pose(neck):
    """Samples describe an angle as seen from ONE camera pose."""
    gaze.record_sample(44, 80, 0.1)
    gaze.record_sample(48, 80, 0.1)
    _fill_dy(-0.4, from_face=True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves and gaze.snapshot() == []


def test_repointing_also_discards_them(body):
    gaze.record_sample(44, 80, 0.1)
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves and gaze.snapshot() == []


def test_repoint_does_not_re_anchor_a_vertical_aim_the_pitch_loop_has_corrected(body):
    """The slow half of the same fight.

    Device-observed: pitch lifted the camera to -31.5 where it could see a
    face, and thirty-four seconds later the repoint anchored idle back on the
    remembered -46.1, from where the face is clipped again. The bearing memory
    is worth believing about which way to face — that is what the move uses —
    but not about how high to look.

    That rule used to cover wrist_pitch alone. Every joint the pitch loop
    steers is a vertical one, so a remembered base_pitch or elbow_pitch undoes
    the correction just as surely; the pitch loop's own anchor holds the
    posture instead.
    """
    anchored = {}
    body.set_idle_anchor = lambda j: anchored.update(j or {})
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._last_pitch_t = gaze.time.monotonic() - config.GAZE_REPOINT_AFTER_S - 1.0
    gaze._maybe_repoint(gaze.time.monotonic())
    assert body.moves                                  # it still turned
    assert not (set(anchored) & set(gaze.PITCH_JOINTS)), (
        f"repoint re-anchored a vertical joint the pitch loop owns: {sorted(anchored)}"
    )


def test_with_no_correction_yet_the_remembered_pose_is_anchored_whole(body, monkeypatch):
    from hal.drivers.tracking import user_bearing

    class _EstWithPitch(_Est):
        pose = {"base_yaw.pos": 4.0, "base_pitch.pos": 0.0, "wrist_pitch.pos": -70.0}

    monkeypatch.setattr(user_bearing, "read_estimate", lambda: _EstWithPitch())
    anchored = {}
    body.set_idle_anchor = lambda j: anchored.update(j or {})
    _absent_for(config.GAZE_REPOINT_AFTER_S + 1)
    gaze._last_pitch_t = 0.0                           # pitch has never spoken
    gaze._last_anchor = None
    gaze._maybe_repoint(gaze.time.monotonic())
    assert anchored.get("wrist_pitch.pos") == pytest.approx(-70.0)


def test_a_face_driven_correction_anchors_the_idle_loop(neck, monkeypatch):
    """Anchoring must not wait for a perfectly centred face.

    Waiting was circular: idle drags the camera back within a cycle, so the
    centred frame that would justify anchoring never arrives and every
    correction is undone before the next one lands.
    """
    anchored = {}
    neck.set_idle_anchor = lambda j: anchored.update(j or {})
    gaze._last_anchor = None
    _fill_dy(-0.4, from_face=True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert anchored.get("wrist_pitch.pos") == pytest.approx(
        neck.moves[0]["wrist_pitch.pos"]
    )


def test_a_guessed_correction_anchors_too_so_idle_does_not_undo_it(neck, monkeypatch):
    """A search whose progress is erased between steps is not a search.

    Leaving the anchor behind during a blind search let idle pull back to where
    the search started — device-observed the head reaching -32.4 and being
    dragged to -41.5 before the next look. The blind budget bounds how far a
    guess can take this; the anchor only stops it being wasted.
    """
    monkeypatch.setattr(config, "GAZE_PITCH_MAX_BLIND_STEPS", 3)
    anchored = {}
    neck.set_idle_anchor = lambda j: anchored.update(j or {})
    gaze._last_anchor = None
    gaze._blind_pitch_steps = 0
    _fill_dy(-0.5, from_face=False)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert anchored.get("wrist_pitch.pos") == pytest.approx(
        neck.moves[0]["wrist_pitch.pos"]
    )


# --- The pitch window: idle's roll sweep must not drive corrections (F25) ---


def test_one_sample_is_not_enough_to_move_the_neck(neck):
    """The bug this window exists for.

    `_maybe_pitch` used to correct from `_last_dy_frac`, a single frame. Idle
    sweeps wrist_roll ~32 deg every ~10s and roll AIMS the camera on this arm,
    so one frame's vertical offset is the framing error plus wherever that
    sweep happens to be. Correcting from it every 4s is how a validated sign
    still walked the head.
    """
    gaze.record_dy(-0.4, True)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == [], "one measurement is not evidence of a framing error"


def test_a_window_shorter_than_an_idle_cycle_is_refused(neck):
    """Half a roll cycle carries the disturbance's mean, not zero."""
    _fill_dy(-0.4, n=12, span=config.GAZE_PITCH_WINDOW_S * 0.5)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == []


def test_a_roll_sweep_around_a_centred_face_does_not_move_the_neck(neck):
    """The disturbance alone must not look like a framing error.

    A face sitting at centre while roll swings the camera through its cycle:
    the samples swing either side of zero, and their median is inside the dead
    zone even though individual frames are well outside it.
    """
    import math
    span = config.GAZE_PITCH_WINDOW_S
    t0 = gaze.time.monotonic() - span
    n = 24
    for i in range(n):
        phase = 2.0 * math.pi * i / n
        gaze.record_dy(0.30 * math.sin(phase), True, now=t0 + span * i / (n - 1))
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves == [], "a periodic disturbance is not a framing error"


def test_a_real_offset_survives_the_same_sweep(neck):
    """...and the signal underneath it still gets corrected, with the right sign."""
    import math
    span = config.GAZE_PITCH_WINDOW_S
    t0 = gaze.time.monotonic() - span
    n = 24
    for i in range(n):
        phase = 2.0 * math.pi * i / n
        # a face 40% above centre, plus the same roll swing as above
        gaze.record_dy(-0.40 + 0.30 * math.sin(phase), True,
                       now=t0 + span * i / (n - 1))
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "a real offset must still be corrected through the noise"
    assert neck.moves[0]["elbow_pitch.pos"] > 5.0, "up is the increasing direction on elbow"


def test_a_correction_clears_the_window_it_was_computed_from(neck):
    """Those offsets describe the pose the camera has just left.

    Clearing is also what spaces corrections apart now: the loop cannot act
    again until a fresh window has refilled.
    """
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert neck.moves, "precondition: the first correction fired"
    assert gaze._dy_estimate() is None, "the window must be empty after a move"


def test_an_unmeasurable_frame_is_not_recorded_as_centred(neck):
    """`None` is 'no evidence', not 'dy = 0' — recording zero would stall it."""
    _fill_dy(-0.4, n=10)
    before = gaze._dy_estimate()
    for _ in range(20):
        gaze.record_dy(None, False)
    after = gaze._dy_estimate()
    assert before is not None and after is not None
    assert before[0] == after[0], "unmeasurable frames must not move the median"


# --- F5: the body is owned while gaze moves it ---


class _OwnershipSvc(_PitchSvc):
    """Records whether the body was OWNED at the instant it was moved.

    `_tracking_active` is the lock `routes/emotion.py` reads to suppress emotion
    servo and the animation loop reads to drop an in-progress recording. Asking
    afterwards proves nothing — ownership is released in a `finally` — so the
    flag has to be sampled from inside the move itself.
    """

    def __init__(self, wrist=-70.0):
        super().__init__(wrist)
        self.owned_during_move = []

    def move_and_hold(self, target, duration=None):
        self.owned_during_move.append(bool(self._tracking_active))
        return super().move_and_hold(target, duration=duration)


def _owned_neck(monkeypatch):
    import hal.app_state as state

    svc = _OwnershipSvc()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "safety_policy", None, raising=False)
    monkeypatch.setattr(config, "GAZE_WAKE_ENABLED", True)
    monkeypatch.setattr(config, "GAZE_PITCH_ENABLED", True)
    # The settle before reading the pose back is there for a real bus; paying
    # it on every correction would add seconds to the suite for nothing.
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 0.0)
    gaze._pitch_stalls.clear()
    gaze._last_pitch_t = gaze.time.monotonic() - 10_000.0
    return svc


def test_a_pitch_correction_owns_the_body_while_it_moves(monkeypatch):
    """Idle writes every joint forever and an emotion re-poses all of them.

    Both were named as candidate causes for this loop walking instead of
    converging, and neither is stopped by anything except this lock.
    """
    svc = _owned_neck(monkeypatch)
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc.moves, "precondition: the correction fired"
    assert svc.owned_during_move == [True], "the move must happen under ownership"


def test_ownership_is_released_after_the_correction(monkeypatch):
    """A watcher that kept the lock would silence emotions for good."""
    svc = _owned_neck(monkeypatch)
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc._tracking_active is False


def test_gaze_still_declines_to_move_a_body_someone_else_owns(monkeypatch):
    """Claiming must not become stealing: a live look or tracking session wins."""
    svc = _owned_neck(monkeypatch)
    svc._tracking_active = True
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert svc.moves == [] and svc.owned_during_move == []


# --- Debug snapshots: a correction has to show what it acted on ---


def test_a_correction_writes_the_frame_it_was_computed_from(neck, tmp_path, monkeypatch):
    """The log says the median was -41%. It cannot say of WHAT.

    Every time this feature was actually understood it was from a picture: the
    clipped-eyes case, the wrong-person aim (F24), the roll experiment. A
    correction that leaves no frame behind is a correction nobody can diagnose.
    """
    import numpy as _np

    monkeypatch.setattr(config, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "GAZE_SNAPSHOT_ENABLED", True, raising=False)
    gaze._last_frame = _np.zeros((90, 160, 3), dtype=_np.uint8)
    gaze._last_box = (10, 5, 40, 40)

    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert neck.moves, "precondition: the correction fired"
    written = list((tmp_path / gaze.SNAPSHOT_CATEGORY).glob("*.jpg"))
    assert len(written) == 1, "one frame per correction"


def test_snapshots_can_be_turned_off(neck, tmp_path, monkeypatch):
    import numpy as _np

    monkeypatch.setattr(config, "SNAPSHOT_PERSIST_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "GAZE_SNAPSHOT_ENABLED", False, raising=False)
    gaze._last_frame = _np.zeros((90, 160, 3), dtype=_np.uint8)

    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert neck.moves, "the correction must still happen"
    assert not (tmp_path / gaze.SNAPSHOT_CATEGORY).exists()


def test_a_failing_snapshot_never_costs_the_correction(neck, monkeypatch):
    """Debug output is never allowed to break the thing it observes."""
    monkeypatch.setattr(
        gaze, "_save_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    _fill_dy(-0.4)
    try:
        gaze._maybe_pitch(gaze.time.monotonic())
    except OSError:
        raise AssertionError("a snapshot failure must not escape _maybe_pitch")


# --- Task C / F11: a repoint must report back to the bearing ---


def _repoint_scored(monkeypatch):
    """Capture what gaze told user_bearing, without touching a real file."""
    calls = []
    from hal.drivers.tracking import user_bearing
    monkeypatch.setattr(
        user_bearing, "record_prediction",
        lambda hit, now=None: calls.append(hit) or False,
    )
    return calls


def test_a_repoint_that_finds_the_user_confirms_the_bearing(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_face_t_before = t - 100.0
    gaze._last_face_t = t - 1.0          # a face was seen AFTER the turn

    gaze._verify_repoint(t)
    assert calls == [True]


def test_a_repoint_that_finds_nobody_counts_against_the_bearing(monkeypatch):
    """The self-healing this feeds is why a moved lamp eventually forgets."""
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_face_t_before = t - 100.0
    gaze._last_face_t = t - 100.0        # nothing seen since the turn

    gaze._verify_repoint(t)
    assert calls == [False]


def test_the_verdict_waits_for_the_settle_window(monkeypatch):
    """Judged too early, every repoint would read as a miss: the move has not
    settled and the sampler has not looked at the new view yet."""
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - 1.0    # only just turned
    gaze._repoint_face_t_before = t - 100.0
    gaze._last_face_t = t - 100.0

    gaze._verify_repoint(t)
    assert calls == [], "too early to judge"


def test_each_repoint_is_scored_exactly_once(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    t = gaze.time.monotonic()
    gaze._repoint_pending_t = t - config.GAZE_REPOINT_VERIFY_S - 1
    gaze._repoint_face_t_before = t - 100.0
    gaze._last_face_t = t - 1.0

    for _ in range(5):
        gaze._verify_repoint(t)
    assert calls == [True], "a repeated verdict would double-count"


def test_nothing_is_scored_when_no_repoint_is_pending(monkeypatch):
    calls = _repoint_scored(monkeypatch)
    gaze._repoint_pending_t = 0.0
    gaze._verify_repoint(gaze.time.monotonic())
    assert calls == []


# --- Task E / F17 + F19: the gate must see a TURN, not a posture ---


def _turn_trail(values, now, span=None):
    """Lay `values` across the WHOLE buffer, oldest first, ending at `now`.

    Distinct from `_trail` above, which fills only the decision window. The
    transition test reads the window before that one, so these tests have to
    span both.
    """
    span = 2.0 * config.GAZE_WINDOW_S if span is None else span
    n = len(values)
    for i, yaw in enumerate(values):
        gaze.record_sample(yaw, 90.0, 0.05, now=now - span + span * i / (n - 1))


def test_a_user_already_facing_the_lamp_is_not_a_gesture(armed, voice):
    """The device failure, reproduced (shadow, 2026-08-24).

    Nine of nine accepted gestures had flat trails like this one. Every one
    would have opened the gate for a user who simply sits square to their desk —
    presence as the signal, which the module docstring says must not happen.
    """
    t = gaze.time.monotonic()
    _turn_trail([13, 12, 12, 11, 12, 11, 13, 21], t)
    assert gaze.on_speech_start() is False


def test_turning_toward_the_lamp_still_opens_the_gate(armed, voice):
    """Away for the baseline, facing for the decision window — a clean turn."""
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)
    assert gaze.on_speech_start() is True


def test_a_turn_caught_mid_buffer_is_marginal_by_construction():
    """Recorded because it bit: the real device trail [90,61,24,15,20,14,7,15]
    lands the turn in the MIDDLE of the buffer, so half the baseline window
    already shows facing and the ratio sits exactly on the threshold.

    That is inherent, not a tuning error — the buffer is only 2x the window, so
    a slow turn spans both. It means GAZE_TRANSITION_MAX_BEFORE trades late
    gestures against steady-facing false positives, and a gesture caught
    half-way may legitimately fail.
    """
    t = 10_000.0
    _turn_trail([90, 61, 24, 15, 20, 14, 7, 15], t)
    before_ratio, before_n = gaze.facing_before(t)
    assert before_n >= config.GAZE_MIN_SAMPLES
    assert 0.4 <= before_ratio <= 0.7, before_ratio


def test_the_transition_test_can_be_turned_off(armed, voice, monkeypatch):
    monkeypatch.setattr(config, "GAZE_REQUIRE_TRANSITION", False)
    t = gaze.time.monotonic()
    _turn_trail([13, 12, 12, 11, 12, 11, 13, 21], t)
    assert gaze.on_speech_start() is True


def test_an_unmeasurable_baseline_does_not_veto(armed, voice):
    """"We could not tell" is not evidence against a turn.

    The baseline is often missing — the buffer is cleared whenever the camera
    moves — and vetoing on that would make the feature fire almost never.
    """
    t = gaze.time.monotonic()
    # samples only inside the decision window; nothing before it
    for i in range(8):
        gaze.record_sample(5.0, 90.0, 0.05,
                           now=t - config.GAZE_WINDOW_S + i * 0.1)
    assert gaze.on_speech_start() is True


def test_blindness_is_reported_as_blind_not_as_looking_away(armed, voice, caplog=None):
    """Different failures, fixed in different places — they must not read alike."""
    import logging as _logging

    records = []
    handler = _logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    gaze.logger.addHandler(handler)
    prev = gaze.logger.level
    gaze.logger.setLevel(_logging.INFO)
    try:
        gaze.on_speech_start()          # empty buffer
    finally:
        gaze.logger.removeHandler(handler)
        gaze.logger.setLevel(prev)

    assert any("-> blind" in m for m in records), records


def test_gaze_asks_for_a_shorter_window_than_a_deliberate_wake(armed, voice):
    """The opener least sure of itself claims the least floor (F20)."""
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)

    assert gaze.on_speech_start() is True
    assert voice.grants == ["gaze"]
    assert voice.windows == [config.GAZE_WAKE_FOCUS_S]


def test_an_older_voice_service_still_gets_a_gate(armed, monkeypatch):
    """Version skew must degrade to the full window, not silence the feature.

    Returning False on a TypeError would look exactly like "nobody ever turns
    to the lamp" — the hardest failure to notice in this whole feature.
    """
    import hal.app_state as state

    class _OldVoice:
        def __init__(self):
            self.grants = []

        def grant_wakeword_focus(self, source="button"):   # no timeout_s
            self.grants.append(source)
            return True

    v = _OldVoice()
    monkeypatch.setattr(state, "voice_service", v, raising=False)
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)

    assert gaze.on_speech_start() is True
    assert v.grants == ["gaze"]


# --- Task E / F21: record WHO, without acting on it yet ---


def test_the_gate_records_who_face_perception_thinks_is_here(armed, voice, monkeypatch):
    """Evidence first. Gaze picks the face nearest frame centre and has no idea
    who spoke; nothing in the log distinguished a correct wake from a colleague
    facing the lamp while the user talked."""
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("leo", 3.0), raising=False)
    assert gaze._who_is_in_frame() == ("leo", 3.0)


def test_an_unknown_face_reports_empty_rather_than_guessing(armed, monkeypatch):
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("", 0.0), raising=False)
    assert gaze._who_is_in_frame() == ("", 0.0)


def test_identity_lookup_never_costs_a_wake(armed, voice, monkeypatch):
    """Face perception is optional — no camera, no presence capability, or a
    failing lookup must not break the gate."""
    import hal.app_state as state

    def _boom():
        raise RuntimeError("perception down")

    monkeypatch.setattr(state, "face_user", _boom, raising=False)
    assert gaze._who_is_in_frame() == ("", 0.0)

    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)
    assert gaze.on_speech_start() is True, "a broken lookup must not veto"


def test_identity_is_observed_not_enforced(armed, voice, monkeypatch):
    """F21 is still OPEN. A colleague in frame must NOT yet change the verdict —
    gating on this before measuring it would be guessing twice."""
    import hal.app_state as state

    monkeypatch.setattr(state, "face_user", lambda: ("someone-else", 1.0), raising=False)
    t = gaze.time.monotonic()
    _turn_trail([90, 88, 85, 90, 12, 10, 8, 9], t)
    assert gaze.on_speech_start() is True


def test_the_watcher_loop_still_calls_the_pitch_correction():
    """Regression: the merge with main dropped this call entirely.

    Main had removed the pitch loop, so its `_loop` had no call to it. Resolving
    the conflict kept the FUNCTION from our side and the CALL SITE from theirs,
    leaving `_maybe_pitch` as dead code — the vertical correction silently did
    nothing on device, and every symptom got misattributed to thresholds.

    A source check rather than a behavioural one on purpose: the failure was
    that nothing called it, which no test of the function itself can catch.
    """
    import inspect

    body = inspect.getsource(gaze._loop)
    assert "_maybe_pitch(now)" in body, "the watcher must drive the pitch correction"
    # Framing before bearing: turning to a bearing that still points at the desk
    # finds nobody however right the bearing is.
    assert body.index("_maybe_pitch(now)") < body.index("_maybe_repoint(now)")


# --- the correction has to actually arrive -------------------------------------
#
# Device-observed 2026-08-25: six consecutive corrections all read
# `elbow_pitch +12.3` and all commanded +25.8. move_and_hold reports nothing, so
# the loop could not tell a completed correction from a failed one and re-sent
# the same unreachable target every ~10s. Only base_pitch's 10% share landed, so
# the offset crept 43% -> 26% instead of closing.


def test_a_joint_that_does_not_arrive_is_benched(neck):
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)      # refuses to lift past +8
    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert "elbow_pitch.pos" in gaze._pitch_stalls, (
        "a joint commanded somewhere it never reached must be noticed"
    )


def test_the_next_correction_routes_around_the_benched_joint(neck):
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    now = gaze.time.monotonic()
    _fill_dy(-0.6)
    gaze._maybe_pitch(now)
    first_elbow_ask = neck.moves[0]["elbow_pitch.pos"]

    gaze._last_pitch_t = now - 10_000.0                 # let it act again
    _fill_dy(-0.6)
    gaze._maybe_pitch(now + 1.0)

    assert len(neck.moves) == 2, "precondition: a second correction fired"
    second = neck.moves[1]
    assert second["elbow_pitch.pos"] <= first_elbow_ask, (
        "the second correction must not ask the stalled joint for more travel"
    )
    # ...and the tilt has to come from somewhere else instead.
    assert second["base_pitch.pos"] < neck.moves[0]["base_pitch.pos"]


def test_a_benched_joint_is_readmitted_once_it_has_rested(neck):
    """Benching is a rest, not a verdict — the elbow that stalled at +17.4
    reached +44 three times running after 60s of quiet."""
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    now = gaze.time.monotonic()
    _fill_dy(-0.6)
    gaze._maybe_pitch(now)
    assert "elbow_pitch.pos" in gaze._pitch_stalls

    neck.stalls.clear()                                  # it cooled down
    lo, hi = gaze._pitch_travel_limits(now + config.GAZE_PITCH_STALL_REST_S + 1.0)
    assert "elbow_pitch.pos" not in lo and "elbow_pitch.pos" not in hi
    assert "elbow_pitch.pos" not in gaze._pitch_stalls


def test_a_joint_that_arrives_is_not_benched(neck):
    _fill_dy(-0.4)
    gaze._maybe_pitch(gaze.time.monotonic())
    assert gaze._pitch_stalls == {}, "a correction that landed is not a stall"


def test_idle_is_anchored_on_where_the_arm_got_to_not_where_it_was_sent(neck):
    """Anchoring an unreached target tells idle to hold a pose the servo has
    already refused — the same command that heats it."""
    neck.stalls["elbow_pitch.pos"] = (-90.0, 8.0)
    anchored = {}
    neck.set_idle_anchor = lambda j: anchored.update(j or {})

    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert anchored, "precondition: the correction anchored idle"
    assert anchored["elbow_pitch.pos"] == pytest.approx(8.0), (
        f"anchored on {anchored['elbow_pitch.pos']:+.1f}, but the elbow stopped at +8.0"
    )


def test_a_move_still_in_flight_is_not_called_a_failure(neck, monkeypatch):
    """The regression the landing check itself introduced.

    A servo has not started moving in the first milliseconds after its goal is
    written, so "two reads the same" is the normal state BEFORE a move as well
    as after. Device-traced: gaze wrote elbow_pitch +30.9 and declared failure
    0.16s later, which would have needed 150 deg/s. It benched a healthy joint
    on every correction and removed it from the allocation.
    """
    monkeypatch.setattr(config, "GAZE_PITCH_SETTLE_S", 2.0)
    reads = {"n": 0}
    real = neck.get_positions

    def slow_arrival():
        # Stays put for the first few polls, then arrives — a real servo.
        reads["n"] += 1
        pose = real()
        if reads["n"] < 4 and neck.moves:
            return dict(pose, **{"elbow_pitch.pos": 5.0})
        return pose

    neck.get_positions = slow_arrival
    _fill_dy(-0.6)
    gaze._maybe_pitch(gaze.time.monotonic())

    assert gaze._pitch_stalls == {}, (
        "a joint that arrived late must not be benched as if it had stalled"
    )
