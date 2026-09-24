"""select_voiced_span: pick the contiguous <=8 s span with the most voice."""

import numpy as np

from hal.drivers.voice.speech_emotion.utils import select_voiced_span

SR = 16_000
MAX_S = 8.0
MAX_N = int(SR * MAX_S)
VOICED_RMS = 2500.0
FRAME_MS = 20


def _clip(total_s: float, bursts: list[tuple[float, float]]) -> np.ndarray:
    """Quiet ramp (RMS well under VOICED_RMS) with loud sine bursts at [start, end) s."""
    n = int(SR * total_s)
    out = (np.arange(n) % 200).astype(np.int16)  # RMS ~115, index-unique pattern
    t = np.arange(n) / SR
    for start, end in bursts:
        mask = (t >= start) & (t < end)
        out[mask] = (10_000 * np.sin(2 * np.pi * 220 * t[mask])).astype(np.int16)
    return out


def _span(samples, out) -> tuple[float, float]:
    """Locate `out` inside `samples` (spans are contiguous slices).

    Scans from the latest offset backwards, not the earliest forwards. The
    synthetic clips built by `_clip` are exactly periodic (the ramp repeats
    every 200 samples, which divides the 16 kHz frame rate, and a sine burst
    at an integer Hz is exactly 1.0 s periodic too), so two windows that are a
    whole number of seconds apart with the same voiced/quiet layout can be
    byte-identical. `select_voiced_span` itself always resolves such ties to
    the latest span (see its docstring), so the locator must prefer the
    latest match too, or it reports a spurious earlier look-alike instead of
    the span the function actually returned.
    """
    for start in range(samples.size - out.size, -1, -320):
        if np.array_equal(samples[start : start + out.size], out):
            return start / SR, (start + out.size) / SR
    raise AssertionError("output is not a contiguous slice of the input")


def _select(samples):
    return select_voiced_span(samples, SR, VOICED_RMS, FRAME_MS, MAX_S)


def test_short_clip_unchanged():
    s = _clip(5.0, [(1.0, 3.0)])
    out = _select(s)
    assert out.size == s.size
    np.testing.assert_array_equal(out, s)


def test_exactly_max_unchanged():
    s = _clip(MAX_S, [(1.0, 3.0)])
    np.testing.assert_array_equal(_select(s), s)


def test_long_clip_crops_to_max_and_contains_burst():
    s = _clip(30.0, [(10.0, 14.0)])
    out = _select(s)
    assert out.size == MAX_N
    start, end = _span(s, out)
    assert start <= 10.0 and end >= 14.0


def test_select_voiced_span_keeps_early_speech():
    s = _clip(30.0, [(0.5, 4.0)])  # speech at the start, silence after
    start, end = _span(s, _select(s))
    assert start <= 0.5 and end >= 4.0


def test_tie_prefers_latest_span():
    s = _clip(30.0, [(2.0, 4.0), (20.0, 22.0)])  # equal voiced mass
    start, end = _span(s, _select(s))
    assert start <= 20.0 and end >= 22.0
    assert start > 4.0


def test_all_silent_long_clip_returns_last_span():
    s = _clip(30.0, [])
    out = _select(s)
    assert out.size == MAX_N
    np.testing.assert_array_equal(out, s[-MAX_N:])
