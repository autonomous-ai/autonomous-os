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
