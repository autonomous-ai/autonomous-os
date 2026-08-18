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
