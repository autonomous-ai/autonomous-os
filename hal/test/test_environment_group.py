"""Multiple environmental components retain independent freshness and faults."""

import json
import logging
import time
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from hal.board.environment import load_environment_components
from hal.board.sen55 import SEN55Timing
from hal.drivers.environment.group import EnvironmentGroup, create_environment_group
from hal.drivers.environment.service import EnvironmentService


def component(sample=None, *, enabled=True, stale=False, state="ready", error=None, age=1):
    worker = Mock()
    worker.snapshot.return_value = {
        "enabled": enabled, "state": state, "stale": stale, "sample": sample,
        "last_error": error, "age_s": age, "bus": 1, "timing": asdict(SEN55Timing()),
    }
    return worker


def test_group_keeps_independent_data_and_provenance():
    sen = component({"timestamp": 100, "temperature_c": 25, "pm2_5_ug_m3": 10, "device_status": 0})
    scd = component({"timestamp": 98, "co2_ppm": 800, "temperature_c": 99}, age=3)
    snap = EnvironmentGroup({"sen55": sen, "scd41": scd}).snapshot()
    assert snap["state"] == "ready" and not snap["stale"]
    assert snap["sample"]["temperature_c"] == 25
    assert snap["sample"]["co2_ppm"] == 800
    assert snap["sources"]["co2_ppm"] == "scd41"
    assert snap["metric_timestamps"] == {"temperature_c": 100, "pm2_5_ug_m3": 100, "co2_ppm": 98}
    assert snap["sample"]["timestamp"] == 100
    assert "device_status" not in snap["sample"]
    assert "bus" not in snap and "timing" not in snap


@pytest.mark.parametrize("fault", ["stale", "error", "status", "missing", "timestamp"])
def test_failed_sen55_does_not_remove_co2(fault):
    sen = component({"timestamp": 100, "pm2_5_ug_m3": 10, "device_status": 0})
    status = sen.snapshot.return_value
    if fault == "stale":
        status["stale"] = True
    elif fault == "error":
        status.update(state="error", last_error="I2C read failed")
    elif fault == "status":
        status["sample"]["device_status"] = 1
    elif fault == "missing":
        status["sample"] = None
    else:
        status["sample"]["timestamp"] = float("nan")
    scd = component({"timestamp": 99, "co2_ppm": 900})
    snap = EnvironmentGroup({"sen55": sen, "scd41": scd}).snapshot()
    assert snap["partial"] and not snap["stale"]
    assert snap["sample"]["pm2_5_ug_m3"] is None
    assert snap["sample"]["co2_ppm"] == 900
    assert "pm2_5_ug_m3" not in snap["metric_timestamps"]


def test_failed_scd41_does_not_remove_sen55():
    sen = component({"timestamp": 100, "pm2_5_ug_m3": 10})
    scd = component({"timestamp": 90, "co2_ppm": 900}, state="error", error="unplugged")
    snap = EnvironmentGroup({"sen55": sen, "scd41": scd}).snapshot()
    assert snap["sample"]["pm2_5_ug_m3"] == 10
    assert snap["sample"]["co2_ppm"] is None
    assert snap["last_error"] == "scd41: unplugged"


def test_single_sen55_retains_legacy_diagnostics():
    sen = component({"timestamp": 100, "pm2_5_ug_m3": 10, "device_status": 0})
    snap = EnvironmentGroup({"sen55": sen}).snapshot()
    assert snap["bus"] == 1 and snap["timing"]["poll_interval_s"] == 1
    assert snap["sample"]["device_status"] == 0


def test_scd41_only_and_group_lifecycle():
    scd = component({"timestamp": 100, "co2_ppm": 600, "humidity_pct": 70})
    group = EnvironmentGroup({"scd41": scd})
    group.start()
    group.stop()
    scd.start.assert_called_once()
    scd.stop.assert_called_once()
    snap = group.snapshot()
    assert snap["sample"] == {"timestamp": 100, "co2_ppm": 600}
    assert snap["bus"] == 1


def test_all_disabled_or_stale():
    assert EnvironmentGroup({}).snapshot()["state"] == "disabled"
    disabled = component(enabled=False, state="disabled")
    snap = EnvironmentGroup({"scd41": disabled}).snapshot()
    assert snap["state"] == "disabled" and snap["sample"] is None
    stale = component({"timestamp": 50, "co2_ppm": 900}, stale=True)
    snap = EnvironmentGroup({"scd41": stale}).snapshot()
    assert snap["stale"] and snap["sample"] is None


def test_component_configuration_and_simulation(tmp_path):
    assert load_environment_components(str(tmp_path)) == ("sen55",)
    (tmp_path / "environment.json").write_text('{"components":["scd41"]}')
    (tmp_path / "scd41.json").write_text('{"boards":{"test":{"bus":2,"automatic_self_calibration":false}}}')
    group = create_environment_group(str(tmp_path), "test")
    worker = group.components["scd41"]
    assert worker.enabled and worker.bus == 2 and worker.timing.poll_interval_s == 5
    assert worker._factory.keywords == {"automatic_self_calibration": False}
    sim = create_environment_group(str(tmp_path), "test", simulation=True)
    assert sim.snapshot()["state"] == "disabled"


@pytest.mark.parametrize("value", [[], {}, {"components": None}, {"components": "scd41"},
    {"components": ["unknown"]}, {"components": [True]}, {"components": ["sen55", "sen55"]},
    {"components": [], "extra": 1}])
def test_invalid_components_rejected(tmp_path, value):
    (tmp_path / "environment.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Invalid environment components"):
        load_environment_components(str(tmp_path))


def test_component_logs_have_stable_sensor_keys(caplog):
    caplog.set_level(logging.INFO, logger="hal.drivers.environment.service")
    EnvironmentService(name="scd41").start()
    assert "[scd41] disabled" in caplog.text
    driver = Mock()
    driver.read.return_value = {"timestamp": 100, "co2_ppm": 700}
    timing = SEN55Timing(poll_interval_s=0.01)
    service = EnvironmentService(True, 1, lambda _: driver, timing, name="scd41")
    service.start()
    deadline = time.monotonic() + 1
    while "[scd41] sample=" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    service.stop()
    assert "[scd41] starting:" in caplog.text
    assert "[scd41] receiving data:" in caplog.text
    assert "[scd41] sample=" in caplog.text
    assert "[scd41] stopped" in caplog.text


def test_waiting_and_retry_logs_are_visible_at_info(caplog):
    caplog.set_level(logging.INFO, logger="hal.drivers.environment.service")
    driver = Mock()
    driver.read.side_effect = [None, OSError("CRC mismatch")]
    service = EnvironmentService(True, 1, lambda _: driver, SEN55Timing(poll_interval_s=0.01), name="sen55")
    service.start()
    deadline = time.monotonic() + 1
    while "[sen55] acquisition failed:" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    service.stop()
    assert "[sen55] waiting for data:" in caplog.text
    assert "[sen55] acquisition failed: CRC mismatch; retry_interval_s=5" in caplog.text
