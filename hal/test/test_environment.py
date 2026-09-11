"""SEN55 wire protocol and hardware-independent acquisition tests."""

import time
from unittest.mock import patch

import pytest

from hal.drivers.environment.sen55 import SEN55, crc8, decode_words
from hal.drivers.environment.service import EnvironmentService


def packet(words):
    return b"".join(w.to_bytes(2, "big") + bytes([crc8(w.to_bytes(2, "big"))]) for w in words)


def test_crc_reference_and_corruption():
    assert crc8(bytes.fromhex("beef")) == 0x92
    assert decode_words(bytes.fromhex("beef92"), 1) == [0xBEEF]
    with pytest.raises(OSError, match="CRC"):
        decode_words(bytes.fromhex("beef00"), 1)
    with pytest.raises(OSError, match="incomplete"):
        decode_words(b"", 1)


def test_wire_read_scales_signed_unknown_and_status():
    driver = SEN55.__new__(SEN55)
    driver._fd = 7
    replies = [packet([1]), packet([123, 456, 789, 0xFFFF, 5000, 64536, 1000, 0x7FFF]), packet([8, 16])]
    with patch("os.write", return_value=2) as write, patch("os.read", side_effect=replies), patch("time.sleep"):
        sample = driver.read()
    assert sample["pm1_0_ug_m3"] == 12.3
    assert sample["pm10_ug_m3"] is None
    assert sample["humidity_pct"] == 50
    assert sample["temperature_c"] == -5
    assert sample["voc_index"] == 100
    assert sample["nox_index"] is None
    assert sample["device_status"] == (8 << 16) | 16
    assert [c.args[1] for c in write.call_args_list] == [b"\x02\x02", b"\x03\xc4", b"\xd2\x06"]


def test_not_ready_does_not_read_measurements():
    driver = SEN55.__new__(SEN55)
    with patch.object(driver, "_command", return_value=[0]) as command:
        assert driver.read() is None
        command.assert_called_once_with(0x0202, 1)


def test_start_rejects_wrong_product():
    driver = SEN55.__new__(SEN55)
    name = b"SEN54".ljust(32, b"\0")
    words = [int.from_bytes(name[i:i + 2], "big") for i in range(0, 32, 2)]
    with patch.object(driver, "_command", side_effect=[[], words]):
        with pytest.raises(OSError, match="Expected SEN55"):
            driver.start()


def test_disabled_and_invalid_config_never_open_bus():
    def unexpected(_):
        pytest.fail("bus must not open")
    service = EnvironmentService(driver_factory=unexpected)
    service.start()
    assert service.snapshot()["state"] == "disabled"
    for bus in (None, "-1", "bad"):
        service = EnvironmentService(True, bus, unexpected)
        service.start()
        assert service.snapshot()["state"] == "error"


def test_worker_sample_and_shutdown():
    class Driver:
        closed = False
        def start(self):
            pass
        def read(self):
            return {"temperature_c": 25}
        def close(self):
            self.closed = True
    driver = Driver()
    service = EnvironmentService(True, "1", lambda _: driver)
    service.start()
    deadline = time.monotonic() + 3
    while service.snapshot()["stale"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.snapshot()["sample"] == {"temperature_c": 25}
    assert not service.snapshot()["stale"]
    service.stop()
    assert driver.closed
    assert service.snapshot()["state"] == "stopped"
    assert service.snapshot()["stale"]


def test_connection_error_and_interruptible_retry():
    def missing(_):
        raise OSError("sensor missing")
    service = EnvironmentService(True, "1", missing)
    service.start()
    deadline = time.monotonic() + 1
    while service.snapshot()["state"] != "error" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.snapshot()["last_error"] == "sensor missing"
    service.stop()
    assert not service._thread.is_alive()


def test_cached_sample_expires_and_is_copied():
    service = EnvironmentService(True, "1")
    service._sample = {"temperature_c": 25}
    service._sample_time = time.monotonic() - 6
    service._state = "ready"
    snapshot = service.snapshot()
    assert snapshot["stale"]
    snapshot["sample"]["temperature_c"] = 0
    assert service.snapshot()["sample"]["temperature_c"] == 25


def test_signed_minus_one_is_not_unavailable():
    driver = SEN55.__new__(SEN55)
    with patch.object(driver, "_command", side_effect=[
        [1], [0xFFFF] * 4 + [0x7FFF, 0xFFFF, 0x7FFF, 0x7FFF], [0, 0],
    ]):
        sample = driver.read()
    assert sample["temperature_c"] == -0.005
    assert sample["humidity_pct"] is None
    assert sample["voc_index"] is None


def test_routes_reject_disabled_stale_and_uninitialized(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType
    from fastapi import HTTPException
    import hal

    fake_state = ModuleType("hal.app_state")
    fake_state.environment_service = None
    monkeypatch.setitem(sys.modules, "hal.app_state", fake_state)
    monkeypatch.setattr(hal, "app_state", fake_state, raising=False)
    spec = importlib.util.spec_from_file_location(
        "environment_route_under_test", Path(__file__).parents[1] / "routes/environment.py",
    )
    route = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(route)
    with pytest.raises(HTTPException) as exc:
        route.environment_status()
    assert exc.value.status_code == 503
    service = EnvironmentService()
    fake_state.environment_service = service
    assert route.environment_status()["state"] == "disabled"
    with pytest.raises(HTTPException) as exc:
        route.environment_sample()
    assert exc.value.status_code == 503
    service._sample = {"temperature_c": 25}
    service._sample_time = time.monotonic()
    service._state = "ready"
    assert route.environment_sample()["sample"]["temperature_c"] == 25
    service._sample_time -= 6
    with pytest.raises(HTTPException) as exc:
        route.environment_sample()
    assert exc.value.status_code == 503


def test_device_wiring_missing_disabled_and_selected_board(tmp_path):
    import json
    from hal.board.sen55 import load_sen55_config

    assert load_sen55_config(str(tmp_path), "board") is None
    path = tmp_path / "sen55.json"
    path.write_text(json.dumps({"boards": {
        "board": {"enabled": True, "bus": 3}, "other": {"enabled": False},
    }}))
    assert load_sen55_config(str(tmp_path), "board").bus == 3
    assert load_sen55_config(str(tmp_path), "other") is None
    assert load_sen55_config(str(tmp_path), "unknown") is None


@pytest.mark.parametrize("data", [
    [], {}, {"boards": []}, {"boards": {"board": []}},
    {"boards": {"board": {"enabled": "false"}}},
    {"boards": {"board": {"enabled": True}}},
    {"boards": {"board": {"bus": True}}},
    {"boards": {"board": {"bus": -1}}},
    {"boards": {"board": {"bus": "1"}}},
    {"boards": {"board": {"bus": 1, "address": 0x69}}},
    {"boards": {"board": {"enabled": False, "bus": -1}}},
])
def test_invalid_device_wiring_rejected(tmp_path, data):
    import json
    from hal.board.sen55 import load_sen55_config

    (tmp_path / "sen55.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid SEN55 wiring"):
        load_sen55_config(str(tmp_path), "board")


def test_lamp_sen55_is_not_declared_or_enabled():
    from pathlib import Path
    from hal.board.device import load_device
    from hal.board.sen55 import load_sen55_config

    robots = Path(__file__).resolve().parents[2] / "robots"
    profile = load_device("lamp", str(robots))
    assert "environment" not in profile.declared_routes()
    for board in profile.boards:
        assert load_sen55_config(str(robots / "lamp"), board) is None


def test_timing_defaults_and_overrides(tmp_path):
    import json
    from hal.board.sen55 import load_sen55_config, SEN55Timing

    assert SEN55Timing() == SEN55Timing(1, 5, 5, 30)
    (tmp_path / "sen55.json").write_text(json.dumps({"boards": {"board": {
        "bus": 3, "poll_interval_s": 2, "retry_interval_s": 8,
        "stale_after_s": 10, "no_data_timeout_s": 60,
    }}}))
    config = load_sen55_config(str(tmp_path), "board")
    assert config.timing == SEN55Timing(2, 8, 10, 60)
    service = EnvironmentService(True, config.bus, timing=config.timing)
    service._sample = {}
    service._sample_time = time.monotonic() - 6
    service._state = "ready"
    assert not service.snapshot()["stale"]


@pytest.mark.parametrize("kwargs", [
    {"poll_interval_s": 0}, {"retry_interval_s": -1},
    {"stale_after_s": float("nan")}, {"no_data_timeout_s": float("inf")},
    {"poll_interval_s": True}, {"retry_interval_s": "5"},
    {"poll_interval_s": 5}, {"no_data_timeout_s": 1},
])
def test_invalid_timing_rejected_even_when_disabled(tmp_path, kwargs):
    import json
    from hal.board.sen55 import load_sen55_config

    (tmp_path / "sen55.json").write_text(json.dumps({"boards": {
        "board": {"enabled": False, **kwargs},
    }}))
    with pytest.raises(ValueError, match="Invalid SEN55 wiring"):
        load_sen55_config(str(tmp_path), "board")


def test_worker_uses_configured_poll_retry_and_timeout():
    from hal.board.sen55 import SEN55Timing
    from unittest.mock import Mock

    driver = Mock()
    driver.read.return_value = None
    timing = SEN55Timing(2, 8, 10, 60)
    service = EnvironmentService(True, 3, lambda _: driver, timing=timing)
    service._stop = Mock()
    service._stop.is_set.return_value = False
    service._stop.wait.side_effect = [False, True]
    with patch("hal.drivers.environment.service.time.monotonic", side_effect=[0, 61]):
        service._run()
    assert [call.args[0] for call in service._stop.wait.call_args_list] == [2, 8]
    driver.close.assert_called_once()


def test_header_pin_metadata(tmp_path):
    import json
    from hal.board.sen55 import load_sen55_config

    path = tmp_path / "sen55.json"
    path.write_text(json.dumps({"boards": {"board": {"bus": 2, "sda_pin": 3, "scl_pin": 5}}}))
    config = load_sen55_config(str(tmp_path), "board")
    assert (config.bus, config.sda_pin, config.scl_pin) == (2, 3, 5)
    for pins in ({"sda_pin": 3}, {"sda_pin": True, "scl_pin": 5}, {"sda_pin": 3, "scl_pin": 3}):
        path.write_text(json.dumps({"boards": {"board": {"enabled": False, **pins}}}))
        with pytest.raises(ValueError, match="Invalid SEN55 wiring"):
            load_sen55_config(str(tmp_path), "board")


def test_null_bus_allowed_only_when_disabled(tmp_path):
    import json
    from hal.board.sen55 import load_sen55_config

    path = tmp_path / "sen55.json"
    entry = {"enabled": False, "bus": None, "sda_pin": 3, "scl_pin": 5}
    path.write_text(json.dumps({"boards": {"board": entry}}))
    assert load_sen55_config(str(tmp_path), "board") is None
    entry["enabled"] = True
    path.write_text(json.dumps({"boards": {"board": entry}}))
    with pytest.raises(ValueError, match="enabled SEN55 requires bus"):
        load_sen55_config(str(tmp_path), "board")
