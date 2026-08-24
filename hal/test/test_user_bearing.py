"""Focused tests for the remembered user bearing.

The estimate is open-loop — nothing corrects it — so the guarantees that matter
are: it converges on real sightings, it reports staleness honestly, it never
writes a torn file, and it rate-limits so one stationary user cannot dominate.
"""

import json
import os
import tempfile
from unittest import mock

import hal.config as config
from hal.drivers.tracking import user_bearing as ub


def _tmp_path(tmpdir):
    return os.path.join(tmpdir, "user_bearing.json")


def _with_path(tmpdir):
    return mock.patch.object(config, "USER_BEARING_PATH", _tmp_path(tmpdir), create=True)


def test_first_sighting_is_taken_verbatim():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.record_sighting(-42.0) is True
        est = ub.read_estimate()
        assert est is not None
        assert est.bearing_deg == -42.0
        assert est.samples == 1


def test_repeated_sightings_converge_on_the_real_position():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(0.0, now=t)
        for i in range(1, 12):
            ub.record_sighting(30.0, now=t + i * 60.0)
        est = ub.read_estimate(now=t + 12 * 60.0)
        assert 28.0 < est.bearing_deg <= 30.0, est.bearing_deg


def test_one_stray_sample_cannot_hijack_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(20.0, now=t + i * 60.0)
        ub.record_sighting(-120.0, now=t + 11 * 60.0)  # someone walking past
        est = ub.read_estimate(now=t + 12 * 60.0)
        assert est.bearing_deg > 0.0, "a single outlier must not flip the sign"


def test_rate_limit_drops_rapid_sightings():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        assert ub.record_sighting(10.0, now=t) is True
        assert ub.record_sighting(10.0, now=t + 1.0) is False
        assert ub.read_estimate(now=t + 1.0).samples == 1


def test_confidence_decays_with_age():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(ub.CONFIDENCE_FULL_SAMPLES):
            ub.record_sighting(15.0, now=t + i * 60.0)
        fresh = ub.read_estimate(now=t + 8 * 60.0).confidence
        stale = ub.read_estimate(now=t + 8 * 60.0 + 48 * 3600).confidence
        assert fresh > 0.9
        assert stale < fresh / 4.0, "a two-day-old estimate must not look authoritative"


def test_no_estimate_reads_as_none_not_zero():
    # Zero is a real bearing (dead ahead) — "unknown" must be distinguishable.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.read_estimate() is None


def test_clear_forgets_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(50.0)
        assert ub.clear() is True
        assert ub.read_estimate() is None
        assert ub.clear() is True  # idempotent


def test_corrupt_file_is_ignored_not_fatal():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        with open(_tmp_path(d), "w") as f:
            f.write("{ this is not json")
        assert ub.read_estimate() is None
        assert ub.record_sighting(5.0) is True


def test_wrong_schema_version_is_ignored():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        with open(_tmp_path(d), "w") as f:
            json.dump({"version": 999, "bearing_deg": 77.0, "samples": 50}, f)
        assert ub.read_estimate() is None


def test_write_is_atomic_no_partial_file_left_behind():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(12.0)
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert leftovers == [], f"temp files left behind: {leftovers}"


def test_a_sustained_move_is_accepted_as_relocation():
    # The flip side of outlier damping: if the user really has moved seat, a
    # repeated new position must win rather than being damped forever.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(20.0, now=t + i * 60.0)
        for i in range(10, 10 + ub.OUTLIER_STREAK):
            ub.record_sighting(-100.0, now=t + i * 60.0)
        est = ub.read_estimate(now=t + 20 * 60.0)
        assert est.bearing_deg < -50.0, f"relocation not accepted: {est.bearing_deg}"


def test_early_sightings_are_not_treated_as_outliers():
    # With one sample the estimate is not settled; a genuinely different second
    # position must still be free to move it.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(0.0, now=t)
        ub.record_sighting(90.0, now=t + 60.0)
        assert ub.read_estimate(now=t + 120.0).bearing_deg > 10.0


# --- prediction-failure detection (how a moved lamp is noticed) -----------

def test_repeated_failed_predictions_drop_the_estimate():
    # No IMU can see the lamp move, so a bearing that stops finding anyone is
    # the only available evidence that it no longer describes reality.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            assert ub.record_prediction(hit=False) is False
            assert ub.read_estimate() is not None
        assert ub.record_prediction(hit=False) is True
        assert ub.read_estimate() is None, "estimate should be dropped"


def test_a_single_miss_does_not_drop_the_estimate():
    # The user simply being out of the room must not wipe a good estimate.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        ub.record_prediction(hit=False)
        assert ub.read_estimate() is not None


def test_a_hit_clears_the_miss_streak():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            ub.record_prediction(hit=False)
        ub.record_prediction(hit=True)
        # Streak reset, so the limit must start over rather than trip immediately.
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            assert ub.record_prediction(hit=False) is False
        assert ub.read_estimate() is not None


def test_scoring_with_no_estimate_is_harmless():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.record_prediction(hit=False) is False
        assert ub.record_prediction(hit=True) is False


def test_misses_spread_far_apart_do_not_accumulate():
    # A user occasionally in another room must not look like a moved lamp.
    # A moved lamp fails every attempt from the moment it moved; scattered
    # absences are a different signal and must not add up across weeks.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(30.0, now=t)
        for i in range(6):
            far_apart = t + (i + 1) * (ub.MISS_STREAK_WINDOW_S + 60.0)
            assert ub.record_prediction(hit=False, now=far_apart) is False
        assert ub.read_estimate(now=t) is not None, "scattered misses must not drop it"


def test_clustered_misses_still_drop_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(30.0, now=t)
        dropped = False
        for i in range(ub.PREDICTION_MISS_LIMIT):
            dropped = ub.record_prediction(hit=False, now=t + i * 300.0)
        assert dropped is True
        assert ub.read_estimate(now=t) is None


def test_a_small_lamp_move_self_corrects_without_being_detected():
    # If the lamp is nudged a little on the desk, the user is still inside the
    # camera FOV at the stale bearing — so every centred sighting records the
    # NEW correct servo yaw and the estimate simply follows. Nothing has to
    # notice the move; only large moves need the miss-streak machinery.
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(30.0, now=t + i * 60.0)
        assert abs(ub.read_estimate(now=t).bearing_deg - 30.0) < 1.0

        # Lamp rotated ~20 deg: same user, new servo yaw. Under OUTLIER_DEG, so
        # it is folded in at full weight rather than being damped as a stray.
        shifted = 50.0
        assert abs(shifted - 30.0) < ub.OUTLIER_DEG
        for i in range(10, 22):
            ub.record_sighting(shifted, now=t + i * 60.0)

        est = ub.read_estimate(now=t + 22 * 60.0)
        assert abs(est.bearing_deg - shifted) < 2.0, (
            f"estimate should have followed the lamp to {shifted}, got {est.bearing_deg}"
        )


# --- Full-posture memory (schema v2) ----------------------------------------

def test_sighting_stores_the_whole_posture(tmp_path, monkeypatch):
    """The bearing must describe a SHAPE, not just a direction — pitch lives
    across base/elbow/wrist and yaw alone cannot aim the camera."""
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    pose = {"base_yaw.pos": 20.0, "base_pitch.pos": 5.0, "elbow_pitch.pos": 10.0}
    assert ub.record_sighting(20.0, pose=pose) is True
    est = ub.read_estimate()
    assert est.pose["base_pitch.pos"] == 5.0
    assert est.pose["elbow_pitch.pos"] == 10.0


def test_bearing_stays_consistent_with_the_pose_yaw(tmp_path, monkeypatch):
    """One source of truth: a scalar bearing that disagreed with the posture
    would point the base one way and the head another."""
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 5.0})
    ub.record_sighting(30.0, pose={"base_yaw.pos": 30.0, "base_pitch.pos": 9.0},
                       now=__import__("time").time() + 60.0)
    est = ub.read_estimate()
    assert est.bearing_deg == est.pose["base_yaw.pos"]


def test_pose_joints_are_smoothed_like_the_bearing(tmp_path, monkeypatch):
    """Each joint gets its own EMA — a single odd posture must not snap the
    remembered shape to it."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 0.0})
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 20.0},
                       now=_t.time() + 60.0)
    pitch = ub.read_estimate().pose["base_pitch.pos"]
    assert 0.0 < pitch < 20.0, f"expected a smoothed pitch, got {pitch}"


def test_v1_file_keeps_its_learned_bearing(tmp_path, monkeypatch):
    """A schema bump must not throw away hours of sightings — v1 had no pose,
    so it migrates with an empty one and refills on the next sighting."""
    path = tmp_path / "b.json"
    path.write_text(json.dumps({
        "version": 1, "bearing_deg": 25.709, "confidence": 0.25,
        "samples": 2, "outlier_streak": 0, "updated": __import__("time").time(),
    }))
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(path), raising=False)
    est = ub.read_estimate()
    assert est is not None, "v1 estimate was discarded"
    assert est.bearing_deg == 25.709
    assert est.pose == {}


def test_relocation_replaces_the_posture_rather_than_averaging_it(tmp_path, monkeypatch):
    """The old shape describes the old place; blending them would aim between."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    t = _t.time()
    for i in range(6):  # settle a confident estimate
        ub.record_sighting(0.0, pose={"base_yaw.pos": 0.0, "base_pitch.pos": 0.0},
                           now=t + i * 60.0)
    t2 = t + 600.0
    for i in range(ub.OUTLIER_STREAK):  # sustained move to a new spot
        ub.record_sighting(80.0, pose={"base_yaw.pos": 80.0, "base_pitch.pos": 30.0},
                           now=t2 + i * 60.0)
    est = ub.read_estimate()
    assert est.pose["base_pitch.pos"] == 30.0, est.pose


def test_a_sighting_without_a_pose_still_moves_the_bearing(tmp_path, monkeypatch):
    """A passive sampler may know the direction but not a trustworthy posture.
    Reading the yaw back out of an unchanged pose would discard the sighting."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(0.0, pose={"base_yaw.pos": 0.0, "base_pitch.pos": 5.0})
    ub.record_sighting(40.0, now=_t.time() + 60.0)  # no pose at all
    est = ub.read_estimate()
    assert est.bearing_deg > 0.0, "the pose-less sighting was ignored"
    assert est.pose["base_pitch.pos"] == 5.0, "the known posture was lost"
    assert est.pose["base_yaw.pos"] == est.bearing_deg


# --- F1: a pose is only valid on the calibration it was recorded against ---


def _seed(d, payload):
    with open(_tmp_path(d), "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _fp(value):
    return mock.patch.object(ub, "_calibration_fingerprint", lambda: value)


def test_a_v2_estimate_is_dropped_because_its_calibration_is_unknown():
    """Every angle is degrees ON A CALIBRATION, and v2 never recorded which.

    `6f0c4ec4` zeroed all five homing offsets, so the same number names a
    different posture afterwards. An unverifiable pose restored at confidence
    1.0 is the failure this schema version exists to stop.
    """
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        _seed(d, {"version": 2, "bearing_deg": 42.0, "pose": {"base_yaw.pos": 42.0},
                  "confidence": 1.0, "samples": 20, "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_v1_estimate_is_dropped_too_not_migrated():
    """v1's yaw used to be kept. The recalibration moved base_yaw's scale as
    well, so the direction is as suspect as the posture."""
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        _seed(d, {"version": 1, "bearing_deg": 42.0, "samples": 20,
                  "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_pose_from_a_different_calibration_is_refused():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        _seed(d, {"version": 3, "calibration": "0000dead", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_pose_from_the_same_calibration_is_kept():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        _seed(d, {"version": 3, "calibration": "beef1234", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        est = ub.read_estimate(now=1_000_001.0)
        assert est is not None and est.bearing_deg == 42.0


def test_an_unreadable_calibration_does_not_wipe_the_estimate():
    """A missing file or a permissions change usually means the arm is not
    running, not that the numbers moved — wiping the fleet's bearings over that
    would be its own bug."""
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp(None):
        _seed(d, {"version": 3, "calibration": "0000dead", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        est = ub.read_estimate(now=1_000_001.0)
        assert est is not None and est.bearing_deg == 42.0


def test_a_sighting_stamps_the_live_calibration():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        ub.record_sighting(10.0, pose={"base_yaw.pos": 10.0})
        with open(_tmp_path(d), encoding="utf-8") as f:
            assert json.load(f)["calibration"] == "beef1234"


def test_the_fingerprint_follows_content_not_timestamp():
    """An OTA rewrites the calibration's mtime without changing an offset."""
    with tempfile.TemporaryDirectory() as d:
        cal = os.path.join(d, "hal.json")
        with open(cal, "w", encoding="utf-8") as f:
            f.write('{"base_yaw": {"homing_offset": 0}}')
        with mock.patch.object(ub, "_calibration_path", lambda: cal):
            first = ub._calibration_fingerprint()
            os.utime(cal, (0, 0))          # same bytes, different timestamp
            assert ub._calibration_fingerprint() == first
            with open(cal, "w", encoding="utf-8") as f:
                f.write('{"base_yaw": {"homing_offset": 1909}}')
            assert ub._calibration_fingerprint() != first
