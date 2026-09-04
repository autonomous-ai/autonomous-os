"""End-of-turn close rule: short clock only, and only from the final's arrival."""

from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.vad_filters import turn_should_close

NOW = 1000.0


def test_no_final_keeps_the_long_fallback_clock():
    assert not turn_should_close(NOW, NOW - 1.5, 0.0)
    assert turn_should_close(NOW, NOW - (voice_cfg.SILENCE_TIMEOUT_S + 0.1), 0.0)


def test_final_shortens_the_wait():
    quiet = NOW - (voice_cfg.ENDPOINT_SILENCE_S + 0.1)
    assert turn_should_close(NOW, quiet, quiet)


def test_a_final_that_just_arrived_never_closes_on_the_same_frame():
    """Below the fallback clock, a fresh final alone must not end the turn."""
    spoke = NOW - (voice_cfg.SILENCE_TIMEOUT_S - 0.1)
    assert not turn_should_close(NOW, spoke, NOW)


def test_a_mid_utterance_final_does_not_close_the_turn_retroactively():
    """Flux emits EndOfTurn on a breath pause ("Hello." before the question).

    The short clock must run from that final, not from the last speech, or the
    session dies on the next frame while the user is still talking.
    """
    spoke = NOW - 0.9  # already quiet for 0.9s when the final lands
    assert not turn_should_close(NOW, spoke, NOW)
    # ...and it does close once the speaker really has stopped for that long.
    assert turn_should_close(
        NOW + voice_cfg.ENDPOINT_SILENCE_S + 0.01, spoke, NOW
    )


def test_zero_disables_the_short_clock(monkeypatch):
    monkeypatch.setattr(voice_cfg, "ENDPOINT_SILENCE_S", 0.0)
    quiet = NOW - (voice_cfg.ENDPOINT_SILENCE_S + 0.1)
    assert not turn_should_close(NOW, NOW - 1.0, NOW - 1.0)
