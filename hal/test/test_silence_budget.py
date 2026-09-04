"""End-of-turn silence budget: short clock once STT has finalized the turn."""

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.vad_filters import silence_budget


def test_no_final_keeps_the_long_fallback_clock():
    assert silence_budget(False) == voice_cfg.SILENCE_TIMEOUT_S


def test_final_shortens_the_wait():
    assert silence_budget(True) == voice_cfg.ENDPOINT_SILENCE_S
    assert voice_cfg.ENDPOINT_SILENCE_S < voice_cfg.SILENCE_TIMEOUT_S


def test_zero_disables_the_short_clock(monkeypatch):
    monkeypatch.setattr(voice_cfg, "ENDPOINT_SILENCE_S", 0.0)
    assert silence_budget(True) == voice_cfg.SILENCE_TIMEOUT_S
