"""Focused tests for the deliberate search sweep.

The behaviours that matter: it looks at the likely place FIRST, its stops
overlap so nobody falls between them, it stays inside the mechanical range,
and it stops the moment it finds someone rather than completing the sweep.
"""

import time
from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
import hal.config as config
from hal.drivers.tracking import constants as C
from hal.drivers.tracking import search


@pytest.fixture(autouse=True)
def _reset():
    search._abort_evt.clear()
    yield


class _FakeCap:
    def __init__(self, frame):
        self._frame = frame

    def acquire_consumer(self):
        pass

    def release_consumer(self):
        pass

    @property
    def last_frame(self):
        return self._frame


class _FakeSvc:
    # The idle recording's first frame, which the animation service holds. The
    # sweep rests on it when there is no bearing to seed from.
    IDLE_BASELINE = {
        "base_yaw.pos": 3.0, "base_pitch.pos": 29.8, "elbow_pitch.pos": 27.1,
        "wrist_pitch.pos": -61.7, "wrist_roll.pos": 8.2,
    }

    def __init__(self, idle_baseline=None):
        self.yaw = 0.0
        self.roll = 0.0
        self.holds = []            # absolute poses restored before the sweep
        # (yaw, roll) after every commanded move, in order. The base turns via
        # nudge() and the head via move_and_hold(), so neither call log alone
        # shows where the camera actually pointed at each step.
        self.trail = []
        # (joint, speed) writes, in order — the sweep caps the base and puts it
        # back, and both halves matter.
        self.speeds = []
        self._idle_baseline = (
            dict(self.IDLE_BASELINE) if idle_baseline is None else idle_baseline
        )
        self.nudge = mock.Mock(side_effect=self._nudge)
        # A real arm reports every joint. `_bearing_step_target` treats a joint
        # ABSENT from the current pose as already-correct, so a double that
        # returns yaw alone can never restore pitch — and would hide the very
        # thing this file now tests.
        self.get_positions = mock.Mock(side_effect=lambda: {
            "base_yaw.pos": self.yaw,
            "base_pitch.pos": 0.0,
            "elbow_pitch.pos": 0.0,
            "wrist_pitch.pos": 0.0,
            "wrist_roll.pos": self.roll,
        })

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos",
                "wrist_pitch.pos", "wrist_roll.pos"]

    UNWRITTEN_SPEED_EQUIVALENT = 175

    def set_joint_speed(self, motor_name, speed):
        self.speeds.append((motor_name, speed))
        return True

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        if "base_yaw.pos" in target:
            self.yaw = float(target["base_yaw.pos"])
        if "wrist_roll.pos" in target:
            self.roll = float(target["wrist_roll.pos"])
        self.trail.append((self.yaw, self.roll))

    def _nudge(self, y, p, d, cur, pol):
        self.yaw += y
        self.trail.append((self.yaw, self.roll))
        return {"base_yaw.pos": self.yaw}


def _run(detect_at_stop=None, bearing=None, disabled=False, abort_at_stop=None,
         confidence=0.9, pose=None, idle_baseline=None, on_progress=None):
    """detect_at_stop: 1-based stop index at which a subject appears."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc(idle_baseline=idle_baseline)
    calls = {"n": 0}

    def _detect(f, t, strict=True):
        # _detect_subject probes "person" then "face" at each stop, so count
        # only the first probe to get the stop number.
        if t == "person":
            calls["n"] += 1
            if abort_at_stop is not None and calls["n"] >= abort_at_stop:
                search.request_abort()
        if detect_at_stop is not None and calls["n"] >= detect_at_stop:
            return (300, 100, 40, 200) if t == "person" else None
        return None

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    if bearing is None:
        est = None
    else:
        est = mock.Mock(bearing_deg=bearing, confidence=confidence)
        # A real estimate carries a whole posture; the seed restores it before
        # sweeping (see _seed_from_bearing).
        est.pose = {"base_yaw.pos": bearing} if pose is None else pose

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det, on_progress=on_progress)
    return res, svc


def test_stops_overlap_so_nobody_falls_between_them():
    """Seams are what a sweep must not have — a person straddling two tiles
    would be missed by both.

    The head covers the gap now, so the base may step further than the lens is
    wide. What has to hold is that one yaw stop's TOTAL reach (widest roll plus
    half the field of view) still overlaps the next stop's.
    """
    reach = max(search.ROLL_STOPS) + config.LOOK_AIM_FOV_DEG / 2.0
    assert search.STEP_DEG < 2 * reach, (
        f"step {search.STEP_DEG} leaves a seam between stops reaching +/-{reach}"
    )


def test_the_sweep_checks_the_remembered_bearing_first():
    """The sweep stops on the FIRST subject it sees, so "first" has to mean the
    person who was asked about.

    Device-observed 2026-08-25 with pure left-to-right ordering: it found a
    person at yaw -102 — a colleague at another desk — while the user sat at the
    seed, -12, which it never reached. Ordering by position alone answers "is
    anyone in this room" when the question was "where are YOU".
    """
    stops = search._stop_list(60.0)
    assert stops[0] == 60.0, "the likely place must be checked first"


def test_the_sweep_goes_right_before_left():
    """The order is what makes the sweep flow.

    The seed stop finishes looking at seed+45, and the RIGHT stop opens on the
    same direction (seed+90 with the head at -45), so the handover is invisible.
    Going left first would throw the head back across everything just covered.
    """
    assert search._stop_list(0.0) == [0.0, search.STEP_DEG, -search.STEP_DEG]


def test_stops_stay_inside_the_mechanical_range():
    for seed in (-135.0, 0.0, 135.0):
        for y in search._stop_list(seed):
            assert C.YAW_MIN <= y <= C.YAW_MAX, f"{y} outside servo limits"


def test_seed_beyond_the_limit_is_clamped():
    stops = search._stop_list(999.0)
    assert max(stops) == C.YAW_MAX
    assert all(C.YAW_MIN <= y <= C.YAW_MAX for y in stops)


def test_a_stop_past_the_limit_is_clamped_not_dropped():
    """With only three stops a discarded one leaves a real hole, whereas a
    clamped one still looks somewhere useful."""
    stops = search._stop_list(C.YAW_MAX - 10.0)
    assert len(stops) == 3, f"a stop was dropped instead of clamped: {stops}"
    assert C.YAW_MAX in stops


def test_stops_on_first_sighting_rather_than_completing_the_sweep():
    res, svc = _run(detect_at_stop=2)
    assert res.found is True
    assert res.stops_visited == 2, "should stop as soon as it sees someone"


def test_reports_failure_after_exhausting_the_sweep():
    res, svc = _run(detect_at_stop=None)
    assert res.found is False
    assert res.reason == "nobody found"
    assert res.stops_visited > 1


def test_camera_disabled_never_sweeps():
    # A search is a lot of conspicuous movement to perform while the user has
    # asked the device not to look.
    res, svc = _run(detect_at_stop=1, disabled=True)
    assert res.found is False
    assert res.reason == "camera disabled"
    assert not svc.nudge.called


def test_abort_stops_the_sweep_mid_flight():
    # request_abort() cancels an in-flight sweep; the flag is cleared at entry
    # so a stale abort cannot prevent the next search from ever running.
    res, svc = _run(detect_at_stop=None, abort_at_stop=2)
    assert res.reason == "aborted"
    assert res.stops_visited < search.MAX_STOPS


def test_a_stale_abort_does_not_block_the_next_search():
    search.request_abort()
    res, _svc = _run(detect_at_stop=1)
    assert res.found is True


# --- Task F / F7: the search restores the posture and honours confidence ---


def test_the_sweep_restores_the_remembered_posture_first():
    """A sweep is the one consumer that provably needs more than yaw.

    It steps the head across up to MAX_STOPS bearings; with the pitch left
    aimed at the desk it sweeps the desk MAX_STOPS times and reports nobody
    there — `user_bearing`'s own warning, applied to the consumer that sweeps
    by definition.
    """
    _res, svc = _run(
        bearing=40.0,
        pose={"base_yaw.pos": 40.0, "base_pitch.pos": -12.0, "wrist_pitch.pos": -30.0},
    )
    assert svc.holds, "no posture was restored before sweeping"
    restored = svc.holds[0]
    assert "wrist_pitch.pos" in restored or "base_pitch.pos" in restored, restored


def test_a_low_confidence_bearing_is_not_used_to_seed_the_sweep():
    """An estimate about to be dropped is not an ordering hint."""
    from hal.drivers.tracking import aim

    res, svc = _run(bearing=120.0, confidence=aim.MIN_BEARING_CONFIDENCE - 0.05)
    seeded_from_bearing = [h for h in svc.holds if h.get("base_yaw.pos") == 120.0]
    assert seeded_from_bearing == [], "a bearing below the floor must not aim the head"
    assert res.stops_visited >= 1


def test_no_bearing_rests_on_the_idle_pose_before_sweeping():
    """Sweeping from wherever the arm happens to be finds nothing.

    A loop that has been walking the head around does not leave it in a pose
    anyone chose, and a sweep from a camera aimed at the desk is thorough about
    the wrong hemisphere. The idle baseline is by construction a pose the lamp
    is designed to rest in — device-checked, it looks out at head height — so
    the "not aimed at the floor" guarantee comes from the pose, not from a
    separate pitch check.
    """
    _res, svc = _run(bearing=None)
    rested = [h for h in svc.holds if h.get("base_pitch.pos") == 29.8]
    assert rested, f"expected a rest on the idle pose, got {svc.holds[:3]}"


def test_with_no_idle_pose_either_the_sweep_starts_where_it_stands():
    """The last resort. A device with neither memory still sweeps rather than
    refusing — half a search beats none."""
    _res, svc = _run(bearing=None, idle_baseline={})
    idle_pitch = _FakeSvc.IDLE_BASELINE["base_pitch.pos"]
    assert [h for h in svc.holds if h.get("base_pitch.pos") == idle_pitch] == []


def test_a_confident_bearing_still_seeds_the_sweep():
    from hal.drivers.tracking import aim

    _res, svc = _run(bearing=40.0, confidence=aim.MIN_BEARING_CONFIDENCE + 0.05)
    assert svc.holds, "a bearing above the floor should be used"


def test_a_failed_posture_restore_still_sweeps():
    """Sweeping from the wrong pitch beats not sweeping at all."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    svc.move_and_hold = mock.Mock(side_effect=RuntimeError("servo busy"))
    det = mock.Mock()
    det.detect = mock.Mock(return_value=None)
    est = mock.Mock(bearing_deg=40.0, confidence=0.9)
    est.pose = {"base_yaw.pos": 40.0, "wrist_pitch.pos": -30.0}

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det)

    assert res.stops_visited >= 1, "a failed restore must not abort the search"


# --- the head looks around at each stop ----------------------------------------


def _rolls(svc):
    """Every wrist_roll angle the sweep commanded, in order."""
    return [h["wrist_roll.pos"] for h in svc.holds if "wrist_roll.pos" in h]


def _sweep_rolls(svc):
    """Just the looks — without the pose restored before the sweep, or the one
    the sweep ends on. Both are deliberate moves home, not part of the search."""
    return _rolls(svc)[1:-1]


def test_each_stop_looks_left_centre_and_right():
    """Turning the whole lamp reads as a camera on a turntable; turning the head
    at a fixed body reads as something looking around. Both cover ground, only
    one of them looks alive."""
    _res, svc = _run(bearing=None)
    rolls = _rolls(svc)

    assert -45.0 in rolls and 0.0 in rolls and 45.0 in rolls
    first = rolls.index(-45.0)
    assert rolls[first:first + 3] == [-45.0, 0.0, 45.0], (
        f"expected left -> centre -> right, got {rolls[:6]}"
    )


def _view_directions(svc):
    """Where the camera pointed after each commanded move: base_yaw + wrist_roll.

    The roll angles alone say nothing about smoothness — a 90 deg swing of the
    head can leave the view exactly where it was if the base moved the other way,
    which is precisely how the seed hands over to the next stop.
    """
    return [yaw + roll for yaw, roll in svc.trail]


def test_the_view_carries_on_from_stop_to_stop():
    """One stop ends where the next begins, so the camera sweeps continuously.

    Exactly one discontinuity is allowed: the far right and the far left are
    genuinely far apart, and no ordering removes that.
    """
    _res, svc = _run(bearing=None)
    views = _view_directions(svc)[1:-1]

    jumps = [abs(b - a) for a, b in zip(views, views[1:])
             if abs(b - a) > search.STEP_DEG + 1e-6]
    assert len(jumps) <= 1, f"more than one discontinuity in {[round(v) for v in views]}"


def test_the_handover_between_the_seed_and_the_next_stop_is_seamless():
    """The base turns +90 while the head turns -90 and the camera does not move.
    That cancellation is the whole reason the right stop comes second."""
    _res, svc = _run(bearing=None)
    views = _view_directions(svc)[1:]

    # The seed's three looks, then the base turn onto the next stop. The turn
    # lands on the same view the last look ended on, which is the point.
    end_of_seed = views[2]
    after_the_turn = views[3]
    assert after_the_turn == pytest.approx(end_of_seed), (
        f"handover jumped {end_of_seed:+.0f} -> {after_the_turn:+.0f} in "
        f"{[round(v) for v in views[:6]]}"
    )


def test_a_subject_found_mid_look_stops_the_sweep_there():
    """The sweep ends on the first subject seen — that is the whole contract,
    and adding a second axis must not make it keep looking past them."""
    res, svc = _run(detect_at_stop=2, bearing=None)
    assert res.found
    assert res.stops_visited == 2, "it kept looking after finding someone"


def test_looking_around_multiplies_the_stops_not_the_yaw_positions():
    """Three looks per yaw stop, so coverage comes from the head rather than
    from turning the body more often."""
    res, svc = _run(bearing=None)
    yaw_stops = len(search._stop_list(_FakeSvc.IDLE_BASELINE["base_yaw.pos"]))
    assert res.stops_visited == yaw_stops * len(search.ROLL_STOPS), (
        f"{res.stops_visited} stops from {yaw_stops} yaw positions"
    )
    assert yaw_stops == 3, "three yaw positions is the whole point of the wider step"


# --- where the sweep leaves the arm --------------------------------------------


def test_a_failed_sweep_returns_to_where_it_started():
    """Nothing found means nothing to look at. Freezing wherever the last look
    left the head leaves the lamp cocked 45 deg over, staring at a wall."""
    _res, svc = _run(bearing=None)
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(
        _FakeSvc.IDLE_BASELINE["wrist_roll.pos"]
    ), f"did not return to the starting pose: {last}"


def test_a_successful_sweep_keeps_looking_at_the_subject():
    """Returning to the seed here would fix the posture and lose the person.

    The head is straightened by turning the BASE as far as the head was turned,
    so the camera ends up pointing at exactly the same place with the head level.
    """
    res, svc = _run(detect_at_stop=1, bearing=None)
    assert res.found
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(0.0), "head left cocked"
    # roll -45 is the first look, so the base must absorb that -45.
    assert last["base_yaw.pos"] == pytest.approx(
        _FakeSvc.IDLE_BASELINE["base_yaw.pos"] + search.ROLL_STOPS[0]
    )


def test_an_abort_also_returns_to_where_it_started():
    """A single click means "stop searching and attend to me".

    The pose an interrupted sweep freezes in is not a resting one — the head can
    be cocked 45 deg over, facing a wall. Stopping there answers the letter of
    the request and none of it: attending to someone means ending somewhere they
    can be seen from.
    """
    res, svc = _run(abort_at_stop=2, bearing=None)
    assert res.reason == "aborted"
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(
        _FakeSvc.IDLE_BASELINE["wrist_roll.pos"]
    ), f"an aborted sweep did not return to its starting pose: {last}"


def test_the_shutter_waits_for_the_arm_to_stop_moving():
    """move_and_hold returns when it has finished SENDING, not when the servos
    have arrived.

    Device-measured: a 90 deg base_yaw turn returns the call in 0.77s and is
    still moving at 5.88s, because base_yaw manages ~14 deg/s under the whole
    lamp's inertia. Without waiting, the head began its looks and the shutter
    fired mid-swing — blurred frames, aimed somewhere other than the stop they
    are recorded against.
    """
    svc = _FakeSvc()
    reads = {"n": 0}
    real = svc.get_positions

    def still_moving():
        # Reports a different yaw for the first few polls, like an arm that has
        # been commanded and is on its way.
        reads["n"] += 1
        pose = dict(real())
        if reads["n"] < 4:
            pose["base_yaw.pos"] = pose["base_yaw.pos"] + 30.0 / reads["n"]
        return pose

    svc.get_positions = still_moving
    t0 = time.monotonic()
    search._wait_until_still(svc, {"base_yaw.pos": 90.0})
    waited = time.monotonic() - t0

    assert reads["n"] >= 4, "it did not keep polling while the arm moved"
    assert waited < search.ARRIVE_TIMEOUT_S, "it waited out the whole timeout"


def test_waiting_gives_up_rather_than_stalling_the_sweep():
    """A stop the arm cannot quite reach is still a fine place to shoot from;
    waiting forever for an arrival that never comes is not."""
    svc = _FakeSvc()
    jitter = {"n": 0}

    def never_settles():
        jitter["n"] += 1
        return {"base_yaw.pos": 100.0 * (jitter["n"] % 2), "wrist_roll.pos": 0.0}

    svc.get_positions = never_settles
    original = search.ARRIVE_TIMEOUT_S
    search.ARRIVE_TIMEOUT_S = 0.4
    try:
        t0 = time.monotonic()
        search._wait_until_still(svc, {"base_yaw.pos": 90.0})
        assert time.monotonic() - t0 < 2.0, "it stalled instead of giving up"
    finally:
        search.ARRIVE_TIMEOUT_S = original


def test_the_sweep_owns_the_body_for_its_whole_duration():
    """Idle plays absolutely, on every joint, and never stops on its own.

    Device-traced during one sweep: idle wrote base_yaw 280 times to the
    search's 31, so every commanded stop was overwritten ~33ms later. The base
    appeared to crawl — 90 deg took 5.9s with HAL running against 0.35s with the
    arm to itself. Not a slow servo, a contested one.
    """
    import hal.app_state as app_state
    from hal.drivers.tracking import aim

    owned_during = []
    real_grab = search._grab_frame

    svc = _FakeSvc()
    svc._tracking_active = False

    def watching(cap):
        owned_during.append(getattr(svc, "_tracking_active", False))
        return real_grab(cap)

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    det = mock.Mock()
    det.detect = mock.Mock(return_value=None)

    with mock.patch.object(app_state, "camera_capture", mock.Mock(), create=True), \
         mock.patch.object(app_state, "animation_service", svc, create=True), \
         mock.patch.object(app_state, "safety_policy", None, create=True), \
         mock.patch.object(search, "_grab_frame", watching), \
         mock.patch.object(search, "_detect_subject", lambda d, f: (None, None, None)):
        search.search_for_subject(detector=det)

    assert owned_during, "the sweep never looked at anything"
    assert all(owned_during), "idle was free to overwrite the sweep's own stops"
    assert not svc._tracking_active, "ownership was not handed back"


def test_a_caller_can_follow_the_sweep_stop_by_stop():
    """A sweep is half a minute of the lamp moving without saying anything.

    The callback exists so a caller can fill that, while WHAT to say — and
    whether to say anything — stays outside this file.
    """
    seen = []
    _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append((v, t)))

    assert seen, "the sweep reported no progress at all"
    assert [v for v, _ in seen] == list(range(1, len(seen) + 1)), seen
    assert all(t == seen[-1][0] for _, t in seen), "the total kept changing"


def test_the_midpoint_of_a_full_sweep_is_the_middle_look():
    """Three yaw stops of three looks each, so #5 — the middle look of the
    middle stop, which is the right-hand stop at roll 0."""
    seen = []
    _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append((v, t)))

    total = seen[-1][1]
    halfway = [v for v, t in seen if v * 2 >= t][0]
    assert total == 9, f"expected 9 looks, got {total}"
    assert halfway == 5, f"midpoint should be look 5, got {halfway}"


def test_a_talkative_caller_cannot_sink_the_sweep():
    def boom(visited, total):
        raise RuntimeError("tts exploded")

    res, _svc = _run(bearing=None, on_progress=boom)
    assert res.stops_visited == 9, "the sweep stopped when the callback threw"


def test_every_sweep_narrates_its_own_midpoint():
    """Whoever started it is waiting through the same silence.

    This used to be passed in by the look-aim, which meant a sweep the USER
    asked for — "where are you?" — ran its full half-minute without a word.
    """
    said = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        _res, _svc = _run(bearing=None)

    assert said == ["look_still_searching"], (
        f"expected exactly one midpoint phrase, got {said}"
    )


def test_a_sweep_that_ends_early_stays_quiet():
    """Found on the second look: there was no long silence to fill."""
    said = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        res, _svc = _run(detect_at_stop=2, bearing=None)

    assert res.found
    assert said == [], f"narrated a sweep that never went quiet: {said}"


def test_a_caller_can_take_over_the_narration():
    """The default speaks; a caller passing its own handler replaces it, and one
    that does nothing keeps the sweep silent."""
    said = []
    seen = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append(v))

    assert seen, "the caller's handler never ran"
    assert said == [], "the default narration fired as well as the caller's"


def test_the_sweep_speeds_the_base_up_only_for_itself():
    """The base has to be brisk during a sweep and unchanged outside it.

    Goal_Speed has to be WRITTEN to take effect — it reads 0 on every joint yet
    the arm behaves as if capped — so this cannot be done once at startup
    without changing how the whole robot moves: idle, emotions, every recorded
    animation. None of that asked to be sped up.
    """
    _res, svc = _run(bearing=None)

    assert svc.speeds, "the sweep never touched the base speed"
    assert svc.speeds[0] == ("base_yaw", search.SWEEP_YAW_SPEED)
    assert svc.speeds[-1][0] == "base_yaw"
    assert svc.speeds[-1][1] != 0, (
        "0 means NO limit — that would leave the base fast for everything after"
    )


def test_the_base_speed_is_restored_even_when_the_sweep_raises():
    """A search that dies part-way must not leave the arm retuned behind it."""
    svc = _FakeSvc()
    with mock.patch.object(search, "_sweep", side_effect=RuntimeError("boom")), \
         mock.patch.object(state, "camera_capture", mock.Mock(), create=True), \
         mock.patch.object(state, "animation_service", svc, create=True), \
         mock.patch.object(state, "safety_policy", None, create=True):
        with pytest.raises(RuntimeError):
            search.search_for_subject(detector=mock.Mock())

    assert len(svc.speeds) == 2, f"speed not restored: {svc.speeds}"
    assert svc.speeds[-1][1] == _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT


def test_the_resting_speed_the_sweep_restores_is_the_one_startup_writes():
    """Two places must agree on what "resting" means, or a killed sweep and a
    clean one leave the arm at different paces.

    Only runs where the servo driver imports — it pulls in lerobot, which is a
    device dependency.
    """
    pytest.importorskip("lerobot")
    from hal.drivers.motors.animation_service import AnimationService

    assert AnimationService._SERVO_REST_SPEED.get(1) == _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT
