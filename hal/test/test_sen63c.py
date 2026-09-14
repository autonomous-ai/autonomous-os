"""SEN63C protocol checks without hardware, against Sensirion's wire format."""

import json
from unittest.mock import patch

import pytest

from hal.board.sen63c import SEN63CTiming, load_sen63c_config
from hal.drivers.environment.i2c import crc8
from hal.drivers.environment.sen63c import SEN63C, FIELDS


def packet(*words):
    return b"".join(w.to_bytes(2, "big") + bytes([crc8(w.to_bytes(2, "big"))]) for w in words)


def identity(value=b"00085700"):
    data = value.ljust(32, b"\0")
    return packet(*(int.from_bytes(data[i:i+2], "big") for i in range(0, 32, 2)))


@pytest.fixture
def driver():
    with patch("os.open", return_value=7) as opened, patch("fcntl.ioctl") as ioctl:
        sensor = SEN63C(2)
    opened.assert_called_once_with("/dev/i2c-2", 2)
    ioctl.assert_called_once_with(7, 0x0703, 0x6B)
    return sensor


def test_constructor_releases_fd_on_ioctl_failure():
    with patch("os.open", return_value=7), patch("fcntl.ioctl", side_effect=OSError("missing")), patch("os.close") as close:
        with pytest.raises(OSError, match="missing"):
            SEN63C(2)
        close.assert_called_once_with(7)


@pytest.mark.parametrize("asc", [None, False, True])
def test_start_checks_product_and_preserves_or_sets_asc(driver, asc):
    driver._automatic_self_calibration = asc
    with patch("os.write", side_effect=lambda fd, data: len(data)) as write, patch("os.read", return_value=identity()), patch("time.sleep") as sleep, patch("os.close") as close:
        driver.start()
        driver.close()
    expected = [b"\x01\x04", b"\xd0\x02"]
    delays = [1.4, 0.02]
    if asc is not None:
        expected.append(b"\x67\x11" + packet(int(asc)))
        delays.append(0.02)
    assert [call.args[1] for call in write.call_args_list] == expected + [b"\x00\x21", b"\x01\x04"]
    assert [call.args[0] for call in sleep.call_args_list] == delays + [0.05, 1.4]
    close.assert_called_once_with(7)


def test_wrong_sen6x_model_does_not_start(driver):
    with patch("os.write", return_value=2) as write, patch("os.read", return_value=identity(b"00085300")), patch("time.sleep"):
        with pytest.raises(OSError, match="Expected SEN63C"):
            driver.start()
    assert len(write.call_args_list) == 2


def test_wire_decoding_and_status(driver):
    with patch("os.write", return_value=2) as write, patch("os.read", side_effect=[packet(1), packet(123, 145, 160, 180, 6543, 65536-1100, 825), packet(0x20, 0x80)]), patch("time.sleep"), patch("time.time", return_value=123):
        assert driver.read() == dict(zip(FIELDS, [12.3, 14.5, 16, 18, 65.43, -5.5, 825]), timestamp=123, device_status=0x200080)
    assert [call.args[1] for call in write.call_args_list] == [b"\x02\x02", b"\x04\x71", b"\xd2\x06"]


def test_ready_ignores_padding_byte(driver):
    with patch.object(driver, "_command", return_value=[0x0100]) as command:
        assert driver.read() is None
        command.assert_called_once_with(0x0202, 1)


def test_co2_warming_preserves_other_metrics(driver):
    with patch.object(driver, "_command", side_effect=[[1], [10, 20, 30, 40, 5000, 4000, 0x7FFF], [0, 0]]):
        sample = driver.read()
    assert sample["co2_ppm"] is None
    assert sample["temperature_c"] == 20
    assert sample["pm2_5_ug_m3"] == 2


def test_all_invalid_sentinels_are_null(driver):
    with patch.object(driver, "_command", side_effect=[[1], [0xFFFF]*4 + [0x7FFF]*3, [0, 0]]):
        sample = driver.read()
    assert all(sample[key] is None for key in FIELDS)
    assert "voc_index" not in sample and "nox_index" not in sample


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("failure", ["short", "crc"])
def test_all_response_stages_validate_length_and_crc(driver, stage, failure):
    responses = [packet(1), packet(10, 20, 30, 40, 5000, 4000, 825), packet(0, 0)]
    responses[stage] = responses[stage][:-1] if failure == "short" else responses[stage][:-1] + bytes([responses[stage][-1] ^ 1])
    with patch("os.write", return_value=2), patch("os.read", side_effect=responses), patch("time.sleep"):
        with pytest.raises(OSError, match="SEN63C: (incomplete|CRC)"):
            driver.read()


def test_short_stop_command_releases_fd(driver):
    with patch("os.write", return_value=1), patch("os.close") as close:
        with pytest.raises(OSError, match="incomplete I2C command"):
            driver.close()
    close.assert_called_once_with(7)


def load_entry(tmp_path, entry):
    (tmp_path / "sen63c.json").write_text(json.dumps({"boards": {"orangepi_sun60": entry}}))
    return load_sen63c_config(str(tmp_path), "orangepi_sun60")


def test_missing_and_disabled_unknown_wiring(tmp_path):
    assert load_sen63c_config(str(tmp_path), "orangepi_sun60") is None
    assert load_entry(tmp_path, {"enabled": False, "bus": None, "sda_pin": None, "scl_pin": None}) is None


def test_enabled_defaults_and_explicit_options(tmp_path):
    config = load_entry(tmp_path, {"enabled": True, "bus": 2})
    assert config.bus == 2
    assert config.timing == SEN63CTiming(1, 5, 5, 30)
    assert config.automatic_self_calibration is None
    config = load_entry(tmp_path, {"bus": 2, "sda_pin": 3, "scl_pin": 5, "automatic_self_calibration": False, "poll_interval_s": 2})
    assert config.automatic_self_calibration is False
    assert config.timing.poll_interval_s == 2


@pytest.mark.parametrize("change", [
    {"bus": None}, {"bus": -1}, {"bus": True}, {"bus": "2"},
    {"enabled": "true"}, {"sda_pin": 3}, {"sda_pin": 3, "scl_pin": 3},
    {"automatic_self_calibration": 1}, {"automatic_self_calibration": "false"},
    {"poll_interval_s": None}, {"poll_interval_s": 0}, {"poll_interval_s": True},
    {"poll_interval_s": float("nan")}, {"retry_interval_s": float("inf")},
    {"stale_after_s": 1}, {"no_data_timeout_s": 1}, {"unknown": 1},
])
def test_invalid_config_rejected(tmp_path, change):
    with pytest.raises(ValueError, match="Invalid SEN63C wiring"):
        load_entry(tmp_path, {"enabled": True, "bus": 2, **change})


def test_disabled_other_board_still_validated(tmp_path):
    (tmp_path / "sen63c.json").write_text(json.dumps({"boards": {
        "orangepi_sun60": {"enabled": False},
        "other": {"enabled": False, "automatic_self_calibration": 1},
    }}))
    with pytest.raises(ValueError, match="automatic_self_calibration"):
        load_sen63c_config(str(tmp_path), "orangepi_sun60")


@pytest.mark.parametrize("data", ["{", "null", "[]", '{"boards": []}', '{"boards": {"x": null}}'])
def test_malformed_document(tmp_path, data):
    (tmp_path / "sen63c.json").write_text(data)
    with pytest.raises(ValueError, match="Invalid SEN63C wiring"):
        load_sen63c_config(str(tmp_path), "orangepi_sun60")
