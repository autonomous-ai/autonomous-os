"""Microphone switch configuration preserves legacy Intern wiring and gating."""

import json
from pathlib import Path

import pytest

from hal.board.privacy_button import PrivacyButtonConfig, load_privacy_button_config


def test_missing_file_and_board_keep_intern_defaults_only(tmp_path):
    for contents in (None, {"boards": {}}):
        if contents is not None:
            (tmp_path / "privacy_button.json").write_text(json.dumps(contents))
        for board in ("orangepi_sun60", "raspberry_pi_5"):
            assert load_privacy_button_config(tmp_path, board, "intern-v2") == PrivacyButtonConfig()
            assert load_privacy_button_config(tmp_path, board, "lamp") is None
            assert load_privacy_button_config(tmp_path, board, "") is None


def test_intern_uses_fallback_and_lamp_declares_pin_11():
    root = Path(__file__).resolve().parents[2] / "robots"
    assert load_privacy_button_config(root / "intern-v2", "orangepi_sun60", "intern-v2") == PrivacyButtonConfig()
    assert not (root / "intern-v2" / "privacy_button.json").exists()
    assert load_privacy_button_config(root / "lamp", "orangepi_sun60", "lamp") == PrivacyButtonConfig(
        chip=1, line=9, disable_camera_on_mute=True, mute_speaker_on_mute=True,
    )
    assert load_privacy_button_config(root / "lamp", "raspberry_pi_5", "lamp") is None


def test_device_override_and_explicit_disable(tmp_path):
    path = tmp_path / "privacy_button.json"
    path.write_text(json.dumps({"boards": {
        "orangepi_sun60": {"chip": 2, "line": 8, "muted_level": 1,
                            "settle_s": .1, "watchdog_s": 5},
        "raspberry_pi_5": {"enabled": False},
    }}))
    assert load_privacy_button_config(tmp_path, "orangepi_sun60", "intern-v2") == PrivacyButtonConfig(2, 8, .1, 1, 5)
    assert load_privacy_button_config(tmp_path, "raspberry_pi_5", "intern-v2") is None


@pytest.mark.parametrize("entry", [
    {"chip": 0, "line": 97, "disable_camera_on_mute": "true"},
    {"chip": 0, "line": 97, "mute_speaker_on_mute": 1},
    {"chip": 0}, {"chip": -1, "line": 97}, {"chip": True, "line": 97},
    {"chip": 0, "line": "97"}, {"chip": 0, "line": 97, "muted_level": 2},
    {"chip": 0, "line": 97, "muted_level": False},
    {"chip": 0, "line": 97, "settle_s": -1},
    {"chip": 0, "line": 97, "settle_s": float("nan")},
    {"chip": 0, "line": 97, "watchdog_s": 0},
    {"chip": 0, "line": 97, "watchdog_s": float("inf")},
    {"chip": 0, "line": 97, "watchdog_s": True},
    {"enabled": "false"}, {"chip": 0, "line": 97, "unknown": 1},
])
def test_invalid_declarations_do_not_silently_fall_back(tmp_path, entry):
    (tmp_path / "privacy_button.json").write_text(json.dumps({"boards": {"orangepi_sun60": entry}}))
    with pytest.raises(ValueError, match="privacy_button.json"):
        load_privacy_button_config(tmp_path, "orangepi_sun60", "intern-v2")


@pytest.mark.parametrize("contents", ["{", "null", '{"boards": []}'])
def test_invalid_file_rejected(tmp_path, contents):
    (tmp_path / "privacy_button.json").write_text(contents)
    with pytest.raises(ValueError, match="privacy_button.json"):
        load_privacy_button_config(tmp_path, "orangepi_sun60", "intern-v2")


def test_legacy_filename_supported_until_device_json_is_renamed(tmp_path):
    (tmp_path / "mic_button.json").write_text(json.dumps({"boards": {
        "orangepi_sun60": {"chip": 1, "line": 9},
    }}))
    assert load_privacy_button_config(tmp_path, "orangepi_sun60", "lamp") == PrivacyButtonConfig(chip=1, line=9)
    (tmp_path / "privacy_button.json").write_text(json.dumps({"boards": {
        "orangepi_sun60": {"enabled": False},
    }}))
    assert load_privacy_button_config(tmp_path, "orangepi_sun60", "lamp") is None
    (tmp_path / "privacy_button.json").write_text("{")
    with pytest.raises(ValueError, match="privacy_button.json"):
        load_privacy_button_config(tmp_path, "orangepi_sun60", "lamp")
