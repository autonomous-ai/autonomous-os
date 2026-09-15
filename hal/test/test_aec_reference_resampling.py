"""Reference filter reuse preserves the existing PCM and playback input."""

from math import gcd
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import scipy.signal

from hal.drivers.voice import aec


@pytest.fixture(autouse=True)
def clear_filter_cache():
    aec._reference_resample_filter.cache_clear()
    yield
    aec._reference_resample_filter.cache_clear()


@pytest.mark.parametrize("src_rate", [44100, 48000])
@pytest.mark.parametrize("frame_count", [1, 137, 441, 480, 1764, 1920, 8191])
def test_cached_filter_matches_default_exactly(src_rate, frame_count, monkeypatch):
    dst_rate = 16000
    samples = np.random.default_rng(7).uniform(-1.2, 1.2, frame_count).astype(np.float32)
    original = samples.copy()
    g = gcd(src_rate, dst_rate)
    up, down = dst_rate // g, src_rate // g
    expected = scipy.signal.resample_poly(samples, up, down)
    coefficients = aec._reference_resample_filter(up, down, samples.dtype.str)
    actual = scipy.signal.resample_poly(samples, up, down, window=coefficients)
    np.testing.assert_array_equal(actual, expected)

    reference = aec.EchoReference(dst_rate)
    monkeypatch.setattr(aec, "_reference", reference)
    monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=dst_rate))
    aec.reference_write(samples.reshape(-1, 1), src_rate)
    expected_pcm = (np.clip(expected, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    pcm, underran = reference.read(len(expected_pcm))
    assert not underran
    assert pcm == expected_pcm
    np.testing.assert_array_equal(samples, original)
    assert not coefficients.flags.writeable


def test_repeated_writes_design_the_filter_once(monkeypatch):
    reference = aec.EchoReference(16000)
    monkeypatch.setattr(aec, "_reference", reference)
    monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=16000))
    with patch.object(scipy.signal, "firwin", wraps=scipy.signal.firwin) as design:
        for size in (441, 137, 1764):
            aec.reference_write(np.zeros(size, dtype=np.float32), 44100)
        # The doubled source and destination rates share the reduced ratio.
        monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=32000))
        aec.reference_write(np.zeros(882, dtype=np.float32), 88200)
        assert design.call_count == 1


def test_filter_cache_separates_dtype_and_is_bounded():
    single = aec._reference_resample_filter(1, 3, np.dtype(np.float32).str)
    double = aec._reference_resample_filter(1, 3, np.dtype(np.float64).str)
    assert single.dtype == np.float32
    assert double.dtype == np.float64
    for down in range(2, 40):
        aec._reference_resample_filter(1, down, np.dtype(np.float32).str)
    assert aec._reference_resample_filter.cache_info().currsize == 32


def test_same_rate_does_not_design_a_filter(monkeypatch):
    reference = aec.EchoReference(16000)
    monkeypatch.setattr(aec, "_reference", reference)
    monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=16000))
    aec.reference_write(np.zeros(160, dtype=np.float32), 16000)
    assert aec._reference_resample_filter.cache_info().misses == 0


def test_prepare_primes_first_write_without_publishing_audio(monkeypatch):
    reference = aec.EchoReference(16000)
    canceller = SimpleNamespace(_rate=16000)
    monkeypatch.setattr(aec, "_reference", reference)
    monkeypatch.setattr(aec, "_canceller", canceller)
    with patch.object(scipy.signal, "firwin", wraps=scipy.signal.firwin) as design:
        aec.prepare_reference(44100)
        assert design.call_count == 1
        assert bytes(reference._buffer) == b""
        assert reference.idle_for() == float("inf")
        assert aec._reference is reference
        assert aec._canceller is canceller
        aec.reference_write(np.zeros(441, dtype=np.float32), 44100)
        assert design.call_count == 1
        assert len(reference._buffer) == 320


@pytest.mark.parametrize("enabled,same_rate", [(False, False), (True, True)])
def test_prepare_noop_does_not_import_or_build_filter(monkeypatch, enabled, same_rate):
    import builtins

    monkeypatch.setattr(aec, "_reference", aec.EchoReference(16000) if enabled else None)
    monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=16000) if enabled else None)
    with patch.object(builtins, "__import__", side_effect=AssertionError("unexpected import")) as importing:
        aec.prepare_reference(16000 if same_rate else 44100)
    importing.assert_not_called()
    assert aec._reference_resample_filter.cache_info().misses == 0


def test_prepare_dependency_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(aec, "_reference", aec.EchoReference(16000))
    monkeypatch.setattr(aec, "_canceller", SimpleNamespace(_rate=16000))
    with patch.object(aec, "_reference_resample_filter", side_effect=ImportError("unavailable")):
        aec.prepare_reference(44100)
    assert bytes(aec._reference._buffer) == b""
