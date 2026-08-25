"""Tests for the request-field -> config.json fallback used by POST /voice/start.

An older os-server binary won't send stt_provider/stt_model/stt_language/
tts_model on the request at all (VoiceStartRequest defaults them to ""), so an
empty request field must still resolve from os-server's config.json instead of
silently losing the setting until os-server is upgraded.
"""

import hal.config as hal_config
from hal.routes.voice import _cfg_fallback


def test_request_value_wins_when_present(monkeypatch):
    def _fail(key, default=""):
        raise AssertionError("should not read config.json when the request already has a value")

    monkeypatch.setattr(hal_config, "_os_cfg_get", _fail)
    assert _cfg_fallback("openai", "stt_provider") == "openai"


def test_empty_request_value_falls_back_to_config_json(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "openai" if key == "stt_provider" else default,
    )
    assert _cfg_fallback("", "stt_provider") == "openai"


def test_empty_request_and_empty_config_value_returns_empty_string(monkeypatch):
    monkeypatch.setattr(hal_config, "_os_cfg_get", lambda key, default="": default)
    assert _cfg_fallback("", "tts_model") == ""


def test_fallback_value_is_stripped(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "  whisper-1  " if key == "stt_model" else default,
    )
    assert _cfg_fallback("", "stt_model") == "whisper-1"
