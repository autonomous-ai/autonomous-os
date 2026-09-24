"""RemoteEmotionRecognizer against new and old perception servers.

HAL always asks for raw probabilities. A new server returns them and HAL gates;
an old server ignores `raw`, gates itself and returns no probabilities, and HAL
must then take its label as-is. Either way a failed gate is None — the caller
records that as no reading.
"""

import json
import types

from hal.drivers.sensing.perceptions.processors import emotion as mod
from hal.drivers.sensing.perceptions.processors.emotion import RemoteEmotionRecognizer
from hal.drivers.sensing.perceptions.processors.emotion_gating import (
    DEFAULT_LABEL_THRESHOLDS,
)

import numpy as np

CROP = np.zeros((40, 30, 3), dtype=np.uint8)


def _recognizer():
    r = object.__new__(RemoteEmotionRecognizer)
    r._url = "http://dl/hal/api/dl/emotion-recognize"
    r._api_key = "k"
    r._threshold = 0.5
    r._timeout = 1.0
    r._crypto = None
    r._debug = None
    r._backoff_until = 0.0
    r._label_thresholds = dict(DEFAULT_LABEL_THRESHOLDS)
    return r


def _serve(monkeypatch, detections):
    sent = []

    def post(url, data, headers, timeout):
        sent.append(json.loads(data))
        return types.SimpleNamespace(
            status_code=200, text="", json=lambda: {"detections": detections}
        )

    monkeypatch.setattr(mod.requests, "post", post)
    return sent


def _det(emotion, confidence, probabilities=None):
    d = {"emotion": emotion, "confidence": confidence, "face_confidence": 1.0,
         "bbox": [0, 0, 30, 40], "valence": None, "arousal": None}
    if probabilities is not None:
        d["probabilities"] = probabilities
    return d


def test_hal_asks_for_raw(monkeypatch):
    sent = _serve(monkeypatch, [])
    _recognizer().recognize(CROP)
    assert sent[0]["raw"] is True


def test_new_server_sad_below_hal_bar_is_no_reading(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.75, {"Neutral": 0.15, "Sad": 0.75, "Happy": 0.10})])
    assert _recognizer().recognize(CROP) is None


def test_new_server_sad_above_hal_bar_stands(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.85, {"Neutral": 0.10, "Sad": 0.85, "Happy": 0.05})])
    result = _recognizer().recognize(CROP)
    assert (result["emotion"], result["gate"]) == ("Sad", "hal")
    assert result["probabilities"]["Sad"] == 0.85


def test_hal_thresholds_are_what_decide(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.75, {"Neutral": 0.15, "Sad": 0.75, "Happy": 0.10})])
    r = _recognizer()
    r._label_thresholds = {"sad": 0.7}
    assert r.recognize(CROP)["emotion"] == "Sad"


def test_old_server_label_is_trusted(monkeypatch):
    # Old server: ignored `raw`, gated with its own map, sent no probabilities.
    _serve(monkeypatch, [_det("Sad", 0.75)])
    result = _recognizer().recognize(CROP)
    assert (result["emotion"], result["gate"]) == ("Sad", "server")


def test_null_probabilities_is_treated_as_old_server(monkeypatch):
    d = _det("Happy", 0.6)
    d["probabilities"] = None
    _serve(monkeypatch, [d])
    assert _recognizer().recognize(CROP)["gate"] == "server"
