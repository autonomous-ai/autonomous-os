"""Tests for the gaze wake trigger.

The angle cases below are built from geometry rather than from captured frames,
so they state what the estimator is supposed to mean rather than re-recording
what it currently outputs.
"""

import math

import pytest

import hal.config as config
from hal.drivers.tracking import gaze


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

    def grant_wakeword_focus(self, source="button"):
        self.grants.append(source)
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
