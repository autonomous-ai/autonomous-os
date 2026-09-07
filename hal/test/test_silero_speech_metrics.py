"""Voiced-ratio metrics, and why a captured turn is judged over its span.

A captured turn is padded: the session prepends VAD pre-roll and keeps a 200ms
tail. That padding costs the same number of chunks whatever was said, so it
dilutes a short utterance far more than a long one — which is how a real
"Yes, that's right." scored 0.500 whole-buffer on lamp-0c89 and was dropped as
noise. span_ratio discounts the padding; sustained noise still fails it.
"""

import threading

import numpy as np
import pytest

from hal.drivers.voice._internal.config import SILERO_CHUNK_SIZE
from hal.drivers.voice._internal.vad_filters import SileroVADFilter


class _ScriptedSession:
    """Silero stand-in returning one scripted confidence per chunk."""

    def __init__(self, confidences):
        self._confidences = list(confidences)
        self._i = 0

    def run(self, _outputs, inputs):
        conf = self._confidences[self._i]
        self._i += 1
        return [np.array([[conf]], dtype=np.float32), inputs["state"]]


def _metrics_for(confidences):
    vad = object.__new__(SileroVADFilter)
    vad._np = np
    vad._lock = threading.Lock()
    vad._session = _ScriptedSession(confidences)
    vad.reset_state()
    # One chunk of silent PCM per scripted confidence — the audio content is
    # irrelevant here, the scripted session decides the verdict.
    pcm = np.zeros(SILERO_CHUNK_SIZE * len(confidences), dtype=np.int16)
    return vad.speech_metrics(pcm, 16000)


def test_padded_short_utterance_survives_on_span():
    # 6 voiced chunks of speech wrapped in 6 chunks of pre-roll and tail: half
    # the buffer, but unbroken speech once the padding is discounted.
    confs = [0.0] * 4 + [0.9] * 6 + [0.0] * 2

    _peak, _mean, ratio, span_ratio, _span_s = _metrics_for(confs)

    assert ratio == pytest.approx(0.5)
    assert span_ratio == pytest.approx(1.0)


def test_sustained_noise_still_fails_on_span():
    # Sparse voiced chunks scattered across the buffer: discounting the padding
    # cannot rescue it, because the gaps are INSIDE the span.
    confs = [0.0, 0.9, 0.0, 0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 0.9, 0.0, 0.0]

    _peak, _mean, ratio, span_ratio, _span_s = _metrics_for(confs)

    assert ratio == pytest.approx(0.25)
    assert span_ratio == pytest.approx(3 / 9)
    assert span_ratio < 0.55


def test_buffer_with_no_voiced_chunk_reports_zero_for_both():
    _peak, _mean, ratio, span_ratio, _span_s = _metrics_for([0.0] * 8)

    assert ratio == 0.0
    assert span_ratio == 0.0


def test_fully_voiced_buffer_scores_one_either_way():
    _peak, mean, ratio, span_ratio, _span_s = _metrics_for([0.9] * 8)

    assert mean == pytest.approx(0.9)
    assert ratio == pytest.approx(1.0)
    assert span_ratio == pytest.approx(1.0)


def test_metrics_fail_open_when_the_model_is_missing():
    vad = object.__new__(SileroVADFilter)
    vad._np = np
    vad._lock = threading.Lock()
    vad._session = None

    assert vad.speech_metrics(np.zeros(512, dtype=np.int16), 16000) == (1.0, 1.0, 1.0, 1.0, 0.0)


def test_span_seconds_measures_the_utterance_not_the_buffer():
    # 6 voiced chunks inside a 12-chunk buffer. The span is the speech alone;
    # the padding must not inflate it. 512 samples @ 16kHz = 32ms per chunk.
    confs = [0.0] * 4 + [0.9] * 6 + [0.0] * 2

    *_rest, span_seconds = _metrics_for(confs)

    assert span_seconds == pytest.approx(6 * 512 / 16000)


def test_span_seconds_is_zero_when_nothing_is_voiced():
    *_rest, span_seconds = _metrics_for([0.0] * 8)

    assert span_seconds == 0.0
