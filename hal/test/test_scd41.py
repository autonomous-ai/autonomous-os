"""Hardware-independent SCD41 protocol and device configuration checks."""

import json
from unittest.mock import patch

import pytest

from hal.board.scd41 import SCD41Timing, load_scd41_config
from hal.drivers.environment.i2c import crc8
from hal.drivers.environment.scd41 import SCD41


def packet(*words):
    return b"".join(w.to_bytes(2, "big") + bytes([crc8(w.to_bytes(2, "big"))]) for w in words)


@pytest.fixture
def driver():
    with patch("os.open", return_value=7), patch("fcntl.ioctl") as ioctl:
        sensor = SCD41(2)
        ioctl.assert_called_once_with(7, 0x0703, 0x62)
    return sensor


def test_constructor_releases_fd_on_ioctl_failure():
    with patch("os.open", return_value=7), patch("fcntl.ioctl", side_effect=OSError("missing")), patch("os.close") as close:
        with pytest.raises(OSError, match="missing"):
            SCD41(2)
        close.assert_called_once_with(7)


def test_periodic_start_and_stop_preserve_calibration(driver):
    with patch("os.write", side_effect=lambda fd, data: len(data)) as write, patch("time.sleep") as sleep, patch("os.close") as close:
        driver.start()
        driver.close()
    assert [call.args[1] for call in write.call_args_list] == [b"\x3f\x86", b"\x21\xb1", b"\x3f\x86"]
    assert [call.args[0] for call in sleep.call_args_list] == [0.5, 0.001, 0.5]
    close.assert_called_once_with(7)


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_asc_setting_has_crc_and_precedes_start(driver, enabled):
    driver._automatic_self_calibration = enabled
    with patch("os.write", side_effect=lambda fd, data: len(data)) as write, patch("time.sleep"):
        driver.start()
    assert [call.args[1] for call in write.call_args_list] == [
        b"\x3f\x86", b"\x24\x16" + packet(int(enabled)), b"\x21\xb1",
    ]


def test_reads_co2_only_after_ready_with_all_words_crc_checked(driver):
    with patch("os.write", return_value=2) as write, patch("os.read", side_effect=[packet(0x8001), packet(825, 26000, 31000)]), patch("time.sleep"), patch("time.time", return_value=123):
        assert driver.read() == {"timestamp": 123, "co2_ppm": 825}
    assert [call.args[1] for call in write.call_args_list] == [b"\xe4\xb8", b"\xec\x05"]


def test_ready_ignores_upper_five_bits(driver):
    with patch.object(driver, "_command", return_value=[0xF800]) as command:
        assert driver.read() is None
        command.assert_called_once_with(0xE4B8, 1)


def test_zero_co2_is_not_a_sample(driver):
    with patch.object(driver, "_command", side_effect=[[1], [0, 123, 456]]):
        assert driver.read() is None


@pytest.mark.parametrize("payload", [b"", packet(800, 25000, 30000)[:-1] + b"\x00"])
def test_rejects_short_or_corrupt_unused_humidity_word(driver, payload):
    with patch("os.write", return_value=2), patch("os.read", side_effect=[packet(1), payload]), patch("time.sleep"):
        with pytest.raises(OSError, match="SCD41: (incomplete|CRC)"):
            driver.read()


def test_short_command_and_stop_error_release_fd(driver):
    with patch("os.write", return_value=1), patch("os.close") as close:
        with pytest.raises(OSError, match="incomplete I2C command"):
            driver.close()
    close.assert_called_once_with(7)


def load_entry(tmp_path, entry):
    (tmp_path / "scd41.json").write_text(json.dumps({"boards": {"orangepi_sun60": entry}}))
    return load_scd41_config(str(tmp_path), "orangepi_sun60")


def test_missing_and_disabled_unknown_wiring(tmp_path):
    assert load_scd41_config(str(tmp_path), "orangepi_sun60") is None
    assert load_entry(tmp_path, {"enabled": False, "bus": None, "sda_pin": None, "scl_pin": None}) is None


def test_enabled_defaults_and_explicit_options(tmp_path):
    config = load_entry(tmp_path, {"enabled": True, "bus": 2})
    assert config.bus == 2
    assert config.timing == SCD41Timing(5, 5, 15, 30)
    assert config.automatic_self_calibration is None
    config = load_entry(tmp_path, {"bus": 2, "sda_pin": 3, "scl_pin": 5, "automatic_self_calibration": False, "poll_interval_s": 10})
    assert config.automatic_self_calibration is False
    assert config.timing.poll_interval_s == 10


@pytest.mark.parametrize("change", [
    {"bus": None}, {"bus": -1}, {"bus": True}, {"bus": "2"},
    {"enabled": "true"}, {"sda_pin": 3}, {"sda_pin": 3, "scl_pin": 3},
    {"automatic_self_calibration": 1}, {"automatic_self_calibration": "false"},
    {"poll_interval_s": None}, {"poll_interval_s": 0}, {"poll_interval_s": True},
    {"poll_interval_s": float("nan")}, {"retry_interval_s": float("inf")},
    {"stale_after_s": 5}, {"no_data_timeout_s": 5}, {"unknown": 1},
])
def test_invalid_config_rejected(tmp_path, change):
    with pytest.raises(ValueError, match="Invalid SCD41 wiring"):
        load_entry(tmp_path, {"enabled": True, "bus": 2, **change})


def test_disabled_other_board_still_validated(tmp_path):
    (tmp_path / "scd41.json").write_text(json.dumps({"boards": {
        "orangepi_sun60": {"enabled": False},
        "other": {"enabled": False, "automatic_self_calibration": 1},
    }}))
    with pytest.raises(ValueError, match="automatic_self_calibration"):
        load_scd41_config(str(tmp_path), "orangepi_sun60")


@pytest.mark.parametrize("data", ["{", "null", "[]", '{"boards": []}', '{"boards": {"x": null}}'])
def test_malformed_document(tmp_path, data):
    (tmp_path / "scd41.json").write_text(data)
    with pytest.raises(ValueError, match="Invalid SCD41 wiring"):
        load_scd41_config(str(tmp_path), "orangepi_sun60")
