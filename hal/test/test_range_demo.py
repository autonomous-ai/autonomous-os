"""The range demo: a narrated tour of what the body can actually do.

What matters: the yaw legs reach the REAL limits (the 54° recording narrated as
a full turn is exactly what this replaces), the pitch legs stay inside travel
the device is known to have, every leg is narrated, and the abort gesture stops
the body and the speech together.
"""

from unittest import mock

import pytest

import hal.app_state as state
from hal.drivers.motors import range_demo
from hal.drivers.tracking import constants as C
from test.body_ownership import BodyOwnership


class _FakeSvc(BodyOwnership):
    # A lamp at rest: head low, which is the posture that has the LEAST room
    # above it. PITCH_LOOK_DEG must still fit.
    REST = {
        "base_yaw.pos": 3.0, "base_pitch.pos": 29.8, "elbow_pitch.pos": 27.1,
        "wrist_pitch.pos": -61.7, "wrist_roll.pos": 8.2,
    }

    UNWRITTEN_SPEED_EQUIVALENT = 0

    def __init__(self):
        self._pos = dict(self.REST)
        self.holds = []
        self.speeds = []

    def get_positions(self):
        return dict(self._pos)

    def set_joint_speed(self, motor_name, speed):
        self.speeds.append((motor_name, speed))
        return True

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        self._pos.update({j: float(v) for j, v in target.items()})
        return dict(self._pos)


@pytest.fixture(autouse=True)
def _reset():
    range_demo._abort_evt.clear()
    range_demo._running.clear()
    yield
    range_demo._abort_evt.clear()
    range_demo._running.clear()


def _run(svc, say=None):
    """Run the demo with the body owned and the waits stubbed out."""
    said = [] if say is None else say
    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", said.append),
        mock.patch.object(range_demo, "_wait_until_still"),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        return range_demo.run(svc), said


def test_the_demo_reaches_the_actual_yaw_limits():
    """The reported bug: "I'll sweep my whole range… doing a full turn now!"
    played a 54.4° recording against a 270° travel. Waypoints computed from the
    limits cannot drift like that."""
    yaws = [p["base_yaw.pos"] for p, _pool in range_demo.waypoints(_FakeSvc.REST)
            if "base_yaw.pos" in p]
    assert min(yaws) == pytest.approx(C.YAW_MIN, abs=0.01)
    assert max(yaws) == pytest.approx(C.YAW_MAX, abs=0.01)
    assert max(yaws) - min(yaws) > 250, (
        f"the demo covers {max(yaws) - min(yaws):.1f}° of "
        f"{C.YAW_MAX - C.YAW_MIN:.0f}°")


def test_the_pitch_legs_stay_inside_travel_the_device_is_known_to_have():
    """WRIST_PITCH_MIN/MAX are ±90 and the arm does not have that: device-
    measured on lamp-ac82, wrist_pitch reached -89.55 going up and only -16.61
    going down. Driving a demo to the declared limit would stall the joint
    against a number nobody has verified, so the legs are a bounded offset from
    the seed — the same one the sweep's look ring already uses every time."""
    seed = _FakeSvc.REST["wrist_pitch.pos"]
    pitches = [p["wrist_pitch.pos"] for p, _pool in range_demo.waypoints(_FakeSvc.REST)
               if "wrist_pitch.pos" in p]
    assert pitches, "the demo never tilts"
    for wp in pitches:
        assert abs(wp - seed) <= range_demo.PITCH_LOOK_DEG + 0.01, (
            f"pitch leg {wp:+.1f} is {abs(wp - seed):.1f}° from the seed — "
            f"beyond the {range_demo.PITCH_LOOK_DEG}° the sweep has proven")
        assert C.WRIST_PITCH_MIN <= wp <= C.WRIST_PITCH_MAX


def test_every_moving_leg_is_narrated():
    """Silent movement is what the canned animation already did."""
    wps = range_demo.waypoints(_FakeSvc.REST)
    assert wps, "no waypoints"
    narrated = [pool for _pose, pool in wps if pool]
    assert len(narrated) == len(wps), "a leg with no phrase"


def test_the_demo_speaks_every_leg_in_order():
    svc = _FakeSvc()
    res, said = _run(svc)
    assert res["completed"] is True
    assert said[0] == "demo_intro"
    assert said[-1] == "demo_done"
    assert said[1:-1] == [pool for _p, pool in range_demo.waypoints(_FakeSvc.REST)]


def test_each_leg_is_announced_then_performed_and_never_overlaps_the_next():
    """The phrase LEADS its own leg — "all the way left" as the base starts
    turning left — so speak-then-move is the right order, not move-then-speak
    (which would narrate what already happened).

    What must not happen is the next leg's phrase arriving while the previous
    leg is still moving. `move_and_hold` returns when it has finished SENDING
    frames, not when the servos arrive — device-measured, a 90° base turn
    returns in 0.77s and is still moving at 5.88s — so without a settle between
    legs the whole script would outrun the body within two legs."""
    svc = _FakeSvc()
    order = []
    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", lambda p: order.append(("say", p))),
        mock.patch.object(range_demo, "_wait_until_still",
                          lambda *_a, **_k: order.append(("settle", None))),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        range_demo.run(svc)

    legs = [pool for _p, pool in range_demo.waypoints(_FakeSvc.REST)]
    at = {v: i for i, (k, v) in enumerate(order) if k == "say"}
    for pool in legs:
        assert pool in at, f"{pool} was never spoken"
        assert order[at[pool] + 1][0] == "settle", (
            f"{pool} was spoken but nothing moved after it")
    for previous, nxt in zip(legs, legs[1:]):
        assert any(order[j][0] == "settle" for j in range(at[previous], at[nxt])), (
            f"{nxt} was announced before {previous}'s movement had finished")


def test_the_base_is_sped_up_for_the_demo_and_put_back():
    """base_yaw manages ~14°/s untouched, so a 135° leg would take ~9s and the
    phrase would finish long before the body did. The sweep already writes this
    register for the same reason — and puts it back, because a cap left behind
    would throttle idle and every emotion."""
    svc = _FakeSvc()
    _run(svc)
    assert svc.speeds, "the demo never touched the base speed"
    assert svc.speeds[0] == ("base_yaw", range_demo.DEMO_YAW_SPEED)
    assert svc.speeds[-1] == ("base_yaw", _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT)


def test_an_abort_stops_the_body_and_the_narration_together():
    """The single click means "stop moving and pay attention to me". A demo
    that keeps talking through it is worse than one that keeps moving."""
    svc = _FakeSvc()
    said = []

    def _say(pool):
        said.append(pool)
        if len(said) == 2:
            range_demo.request_abort()

    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", _say),
        mock.patch.object(range_demo, "_wait_until_still"),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        res = range_demo.run(svc)

    assert res["completed"] is False
    assert res["reason"] == "aborted"
    assert len(said) <= 3, f"kept narrating after the abort: {said}"
    assert svc.holds[-1] == _FakeSvc.REST, "an aborted demo must go home"


def test_the_demo_ends_where_it_started():
    svc = _FakeSvc()
    _run(svc)
    assert svc.holds[-1] == _FakeSvc.REST


def test_a_sleeping_device_does_not_perform():
    svc = _FakeSvc()
    with mock.patch.object(state, "_sleeping", True, create=True):
        res = range_demo.start(svc)
    assert res["started"] is False
    assert res["reason"] == "sleeping"
    assert svc.holds == []


def test_a_second_demo_does_not_start_on_top_of_a_running_one():
    """Two performances sharing one body is two half-performances."""
    svc = _FakeSvc()
    range_demo._running.set()
    with mock.patch.object(state, "_sleeping", False, create=True):
        res = range_demo.start(svc)
    assert res["started"] is False
    assert res["reason"] == "already running"
    assert svc.holds == []


def test_start_returns_immediately_rather_than_performing():
    """The agent reaches this through a [HW:...] marker, and fireHWCall gives a
    hardware POST five seconds while the demo runs ~20. A blocking route would
    time out mid-performance and log nothing at all."""
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "_sleeping", False, create=True),
        mock.patch.object(range_demo.threading, "Thread") as thread,
    ):
        res = range_demo.start(svc)
    assert res["started"] is True
    assert res["waypoints"] == len(range_demo.waypoints(_FakeSvc.REST))
    thread.assert_called_once()
    assert thread.call_args.kwargs["daemon"] is True
    assert svc.holds == [], "start() performed the demo on the caller's thread"


def _demo_client():
    """A TestClient over the servo router — no hardware, no app startup."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hal.routes.servo import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_route_starts_the_demo_and_returns_at_once():
    with (
        mock.patch("hal.routes.servo._sleep_servo_locked", return_value=False),
        mock.patch("hal.routes.servo._svc_connected") as svc,
        mock.patch("hal.drivers.motors.range_demo.start",
                   return_value={"started": True, "waypoints": 4,
                                 "reason": "started"}) as start,
    ):
        svc.return_value.is_suppressed = False
        body = _demo_client().post("/servo/demo").json()

    start.assert_called_once()
    assert body == {"status": "ok", "started": True, "waypoints": 4,
                    "reason": "started"}


def test_the_route_refuses_while_the_device_sleeps():
    """Sleep is a terminal state — nothing external touches the servos until a
    wake emotion clears it. A demo is about as external as it gets."""
    with (
        mock.patch("hal.routes.servo._sleep_servo_locked", return_value=True),
        mock.patch("hal.drivers.motors.range_demo.start") as start,
    ):
        body = _demo_client().post("/servo/demo").json()

    start.assert_not_called()
    assert body["started"] is False
    assert body["reason"] == "sleeping"


def test_the_single_click_aborts_the_demo_with_the_aim_and_the_sweep():
    """Wired where the other two aborts already are. A click that stopped the
    arm but not the narration would leave the lamp describing legs it is no
    longer performing."""
    from hal.drivers import button_actions

    with (
        mock.patch("hal.drivers.motors.range_demo.request_abort") as abort_demo,
        mock.patch("hal.drivers.tracking.aim.request_abort"),
        mock.patch("hal.drivers.tracking.search.request_abort"),
        mock.patch.object(state, "tracker_service", None),
    ):
        button_actions._stop_active_tracking("test")

    abort_demo.assert_called_once()
