"""Noise guard for turns whose STT invented a short word out of room noise."""

import pytest

from hal import config as hal_config
from hal.drivers.voice._internal.realtime_turn import is_noise_turn, needs_noise_guard


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_REQUIRE_TRANSCRIPT", True)
    monkeypatch.setattr(hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 3)


def test_guard_runs_for_empty_and_short_transcripts():
    assert needs_noise_guard("")
    assert needs_noise_guard("Okay")
    assert needs_noise_guard("Thank you very")


def test_guard_skipped_for_a_real_sentence():
    assert not needs_noise_guard("bật đèn lên giúp anh")


def test_guard_disabled_leaves_transcript_turns_alone(monkeypatch):
    monkeypatch.setattr(hal_config, "REALTIME_NOISE_GUARD_MAX_WORDS", 0)
    assert not needs_noise_guard("Okay")
    assert not is_noise_turn("Okay", 2.0, audio_is_speech=False)


def test_short_transcript_over_noise_is_dropped():
    # What reached the backend as a "turn of pure noise": STT fabricated a filler,
    # Silero says the buffer was never voiced.
    assert is_noise_turn("Okay", 2.0, audio_is_speech=False)


def test_short_transcript_over_real_speech_commits():
    assert not is_noise_turn("bật đèn", 2.0, audio_is_speech=True)


def test_long_transcript_commits_even_if_silero_disagrees():
    # A full sentence is never treated as a fabrication — the voiced-ratio floor
    # must not silence a real utterance it happened to score low.
    assert not is_noise_turn("bật đèn lên giúp anh", 2.0, audio_is_speech=False)


def test_missing_guard_result_never_drops_a_turn():
    # audio_is_speech defaults to True for callers that did not run the guard.
    assert not is_noise_turn("Okay", 2.0)


def test_empty_transcript_still_dropped_by_require_transcript():
    assert is_noise_turn("", 2.0, audio_is_speech=True)
