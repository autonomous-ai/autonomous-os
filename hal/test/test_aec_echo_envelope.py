"""Envelope test that separates the lamp's own echo from a real interruption.

The level gate provably cannot do this: measured on device 27/08/2026 the
silent-room echo ceiling sat ABOVE real interruptions at every speaker volume
(25%: 9804 vs 8027, 40%: 9969 vs 6956-8027, 65%: 13560 vs 6956). These tests
pin the property that replaces it — echo tracks the reference's loudness
contour, a person does not — including at echo levels far below the reference,
which is where a threshold would already have given up.
"""

import numpy as np
import pytest

from hal.drivers.voice import aec


RATE = 16000
WINDOW_MS = 384  # BARGE_IN_SPEECH_FRAMES (6) x FRAME_DURATION_MS (64)


def _syllables(n_ms, rate=RATE, seed=0, period_ms=200, amplitude=8000.0):
    """Speech-like audio: a carrier gated by a syllable-rate envelope."""
    rng = np.random.default_rng(seed)
    n = int(rate * n_ms / 1000)
    t = np.arange(n) / rate
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * (1000.0 / period_ms) * t)
    carrier = rng.standard_normal(n)
    return (amplitude * envelope * carrier).astype(np.int16)


class _FakeCanceller:
    """Only what echo_envelope_match reads: the rate and the raw-mic history."""

    def __init__(self, rate=RATE):
        self._rate = rate
        self._mic_history = bytearray()
        self._clean_history = bytearray()

    def hear(self, pcm: np.ndarray, cancelled: np.ndarray = None) -> None:
        """Feed one window. `cancelled` defaults to no suppression at all."""
        self._mic_history.extend(pcm.tobytes())
        self._clean_history.extend(
            (pcm if cancelled is None else cancelled).tobytes()
        )


@pytest.fixture
def wired(monkeypatch):
    reference = aec.EchoReference(RATE, max_ms=500, history_ms=aec._HISTORY_MS)
    canceller = _FakeCanceller()
    monkeypatch.setattr(aec, "_reference", reference)
    monkeypatch.setattr(aec, "_canceller", canceller)
    # Module-level learned coupling — reset so test order cannot change verdicts.
    monkeypatch.setattr(aec, "_coupling_db", None)
    return reference, canceller


def test_echo_scores_high_even_when_far_quieter_than_the_reference(wired):
    """A 30dB-down, delayed copy still matches: shape is what is compared."""
    reference, canceller = wired
    played = _syllables(700, seed=1)
    reference.write(played.tobytes())

    delay = int(RATE * 0.205)  # the measured speaker->mic delay on lamp
    canceller.hear((played[delay:] * 0.03).astype(np.int16))

    score = aec.echo_envelope_match(WINDOW_MS, np)
    assert score is not None
    assert score > 0.65, f"echo scored {score}, would be let through as a person"


def _echo_plus_person(played, person_fraction=0.4, person_gain=8.0):
    """Mic during a real interruption: echo throughout, person ADDED partway.

    Not "person instead of echo" — during drain the speaker never stops, so the
    person always arrives on top. They cover part of the window, which is what
    lifts the residual's top tail without moving its median.
    """
    delay = int(RATE * 0.205)
    echo = played[delay:].astype(np.float32) * 0.03
    mic = echo.copy()
    start = int(len(mic) * (1.0 - person_fraction))
    person = _syllables(
        (len(mic) - start) * 1000 // RATE + 1, seed=99, period_ms=310
    )[: len(mic) - start].astype(np.float32)
    rms = float(np.sqrt(np.mean(echo ** 2)))
    person *= person_gain * rms / float(np.sqrt(np.mean(person ** 2)))
    mic[start:] += person
    return mic.astype(np.int16)


def test_person_added_on_top_of_the_echo_scores_low(wired):
    """The case that matters: someone interrupts while the reply plays on."""
    reference, canceller = wired
    played = _syllables(700, seed=1)
    reference.write(played.tobytes())
    canceller.hear(_echo_plus_person(played))

    score = aec.echo_envelope_match(WINDOW_MS, np)
    assert score is not None
    assert score < 0.65, f"a real interruption scored {score}, would be rejected"


def test_reads_raw_mic_not_what_the_caller_holds(wired):
    """The caller only ever holds cancelled audio, which cannot be judged.

    Cancellation is a time-varying gain, so it eats the very contour this
    compares — measured on device, well-cancelled echo scored 0.42/0.45 and
    leaked through. The window must come from the pre-APM history.
    """
    reference, canceller = wired
    played = _syllables(700, seed=1)
    reference.write(played.tobytes())
    delay = int(RATE * 0.205)
    canceller.hear((played[delay:] * 0.03).astype(np.int16))

    # No audio is passed in at all — a caller cannot influence the verdict.
    assert aec.echo_envelope_match(WINDOW_MS, np) > 0.65


def test_window_is_the_tail_not_the_whole_history(wired):
    """Old clean echo must not vouch for a candidate that arrived after it."""
    reference, canceller = wired
    played = _syllables(1400, seed=1)
    reference.write(played.tobytes())
    delay = int(RATE * 0.205)
    early = played[delay: delay + int(RATE * 0.6)]
    canceller.hear((early * 0.03).astype(np.int16))  # clean echo, then:
    canceller.hear(_echo_plus_person(played[delay + int(RATE * 0.6):]))

    assert aec.echo_envelope_match(WINDOW_MS, np) < 0.65


def test_near_perfect_fit_is_echo_whatever_the_residual_says(wired):
    """Device-observed: corr=0.96 at lag=240ms was the lamp's own sentence.

    The residual read only 2.1dB there and let it through, cutting the reply
    off mid-word. A person cannot produce a near-perfect fit to the reply they
    are talking over — real interruptions measured 0.75-0.88 in this domain.
    """
    reference, canceller = wired
    played = _syllables(700, seed=1)
    reference.write(played.tobytes())
    delay = int(RATE * 0.205)
    canceller.hear((played[delay:] * 0.03).astype(np.int16))

    assert aec.echo_envelope_match(WINDOW_MS, np) == 1.0


def test_labelled_device_measurements_land_on_the_right_side(wired):
    """Pin the cut against the labelled run of 27/08/2026 (lamp-0c89, 40%).

    Echo windows measured -50.0..+4.8dB of skew, confirmed interruptions
    +8.4..+40.4dB. Feeding the skew straight through the same mapping the
    function uses keeps the two populations on opposite sides of the default
    threshold, so a later tweak to the constants cannot silently reopen the
    gap that made the lamp cut itself off.
    """
    def score_for(skew_db):
        return 1.0 / (
            1.0
            + max(0.0, skew_db - aec._SKEW_ECHO_FLOOR_DB) / aec._EXCESS_HALF_DB
        )

    threshold = 0.65  # BARGE_IN_ECHO_MATCH default
    for echo_skew in (-50.0, -9.7, 0.0, 1.3, 4.8):
        assert score_for(echo_skew) >= threshold, echo_skew
    for person_skew in (8.4, 14.4, 26.9, 40.4):
        assert score_for(person_skew) < threshold, person_skew


def test_loud_person_filling_the_window_is_caught_by_coupling(wired):
    """The person the skew test structurally cannot see.

    Someone who talks over the WHOLE window shifts the residual bodily, and
    subtracting its median takes them out along with the echo — device-observed
    27/08/2026, an RMS=20452 interruption scored 0.9dB of skew and was rejected
    against an echo ceiling near 10000. What still gives them away is the
    speaker->mic coupling: echo holds it steady, they blow past it.
    """
    reference, canceller = wired
    played = _syllables(1600, seed=1)
    reference.write(played.tobytes())
    delay = int(RATE * 0.205)
    echo = played[delay:].astype(np.float32) * 0.03

    # Echo-only windows first, so the coupling baseline is learned.
    for _ in range(6):
        canceller.hear(echo[: int(RATE * 0.4)].astype(np.int16))
        assert aec.echo_envelope_match(WINDOW_MS, np) is not None
    assert aec._coupling_db is not None

    # Now the same window, but 10dB louder than the echo path allows.
    canceller.hear((echo[: int(RATE * 0.4)] * 10 ** (10 / 20.0)).astype(np.int16))
    assert aec.echo_envelope_match(WINDOW_MS, np) == 0.0


def test_returns_none_without_enough_reference(wired):
    """Unknown, not clean — the caller must not read None as permission."""
    reference, canceller = wired
    reference.write(_syllables(20, seed=1).tobytes())
    canceller.hear(_syllables(WINDOW_MS, seed=2))
    assert aec.echo_envelope_match(WINDOW_MS, np) is None


def test_returns_none_when_nothing_was_played(wired):
    _, canceller = wired
    canceller.hear(_syllables(WINDOW_MS, seed=2))
    assert aec.echo_envelope_match(WINDOW_MS, np) is None


def test_returns_none_with_no_canceller(monkeypatch):
    monkeypatch.setattr(aec, "_canceller", None)
    monkeypatch.setattr(aec, "_reference", None)
    assert aec.echo_envelope_match(WINDOW_MS, np) is None


def test_history_survives_the_fifo_being_drained(wired):
    """process() drains the FIFO; the envelope test must still see the audio."""
    reference, _ = wired
    played = _syllables(700, seed=1).tobytes()
    reference.write(played)
    drained, _underran = reference.read(len(played))

    assert len(drained) == len(played)
    assert reference.read(320)[1] is True, "FIFO should now be empty"
    assert len(reference.history()) == len(played)


def test_history_is_bounded(wired):
    reference, _ = wired
    reference.write(_syllables(2000, seed=1).tobytes())
    assert len(reference.history()) == reference._history_max


def test_clear_drops_history_too(wired):
    """A route change invalidates the reference — stale audio would misjudge."""
    reference, _ = wired
    reference.write(_syllables(700, seed=1).tobytes())
    reference.clear()
    assert reference.history() == b""
