"""How much of the window a facial emotion must hold before it becomes an event.

A man sitting at his desk generated 41 "Anger" flush emissions in 29 minutes and
not one was correct: a face turned toward a monitor reads as Anger to an
AffectNet-trained model. The vote let it through because it counted only the
frames that came back with a label — a lone Anger frame among a dozen attempts
that returned nothing won outright, since `_flush_buffer` dropped Neutral and
then took `most_common(1)`.

Two rules follow, and they are asymmetric on purpose:

  * a failed recognition still happened, so it belongs in the denominator. An
    empty response is not evidence of Neutral — the service gates the argmax per
    label and hands back Neutral's own low probability — it is "no confirmed
    reading", and a negative label has to outweigh those too.
  * Happy keeps firing on one frame. At ~2.2s between triggers a smile is
    frequently a single frame, and requiring persistence dropped most of the
    genuine ones. The noisy labels sit on the other side of the line.
"""

import threading
import time
import types

import numpy as np

from hal.drivers.sensing.perceptions.processors.emotion import (
    _NO_READING,
    _OCCUPANCY_LOOKBACK_S,
    EmotionData,
    EmotionPerception,
)

PERSON = "long"


class _Sidecar:
    def save(self, _state):
        pass


def _perception(readings, attempts, attempt_age_s=1.0):
    """An EmotionPerception carrying only the state `_flush_buffer` reads.

    Built with __new__ so the test needs no network, crypto session or config —
    the method under test is the real one.
    """
    p = object.__new__(EmotionPerception)
    p._state_lock = threading.RLock()
    p._flush_interval = 10.0
    p._last_flush_ts = 0.0          # far enough in the past that a flush is due
    p._presence_service = None
    p._perception_state = types.SimpleNamespace(
        current_user=types.SimpleNamespace(data=PERSON)
    )
    p._last_sent_by_key = {}
    p._last_sent_key = None
    p._last_sent_ts = 0.0
    p._dedup_window_s = 300.0
    p._sidecar = _Sidecar()

    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    face = types.SimpleNamespace(bbox=[0, 0, 10, 10], person_id=PERSON, confidence=0.9)
    p._emotion_buffer = {
        PERSON: [
            EmotionData(frame=frame, face=face, emotion=e, confidence=0.7)
            for e in readings
        ]
    }
    now = time.time()
    p._attempt_history = [(now - attempt_age_s, PERSON, a) for a in attempts]

    sent = []
    p._send_event = lambda *args, **kwargs: sent.append(args)
    return p, sent


def _labels(sent):
    """The emotion named in each emitted message."""
    return [a[1].split(":")[1].split(".")[0].strip() for a in sent]


def test_a_single_happy_frame_still_becomes_an_event():
    """Unchanged behaviour: a smile is usually one frame and must survive."""
    p, sent = _perception(["Happy"], ["Happy"] + [_NO_READING] * 9)
    p._flush_buffer()
    assert _labels(sent) == ["Happy"]


def test_a_single_anger_frame_among_failures_is_dropped():
    """The exact shape that produced the false events."""
    p, sent = _perception(["Anger"], ["Anger"] + [_NO_READING] * 9)
    p._flush_buffer()
    assert sent == []


def test_anger_holding_the_window_still_fires():
    """The gate is occupancy, not a ban — a sustained read is still reported."""
    p, sent = _perception(["Anger"] * 4, ["Anger"] * 4)
    p._flush_buffer()
    assert _labels(sent) == ["Anger"]


def test_failed_attempts_count_against_a_negative():
    """The heart of the change: same three readings, different denominator.

    Three Anger among three failures is 3/6 — under the 2/3 bar — where the old
    vote saw only the three Anger frames and emitted.
    """
    p, sent = _perception(["Anger"] * 3, ["Anger"] * 3 + [_NO_READING] * 3)
    p._flush_buffer()
    assert sent == []

    p, sent = _perception(["Anger"] * 3, ["Anger"] * 3)
    p._flush_buffer()
    assert _labels(sent) == ["Anger"]


def test_a_stale_negative_does_not_carry_over():
    """Attempts older than the lookback are not evidence about now."""
    p, sent = _perception(
        ["Anger"] * 4, ["Anger"] * 4, attempt_age_s=_OCCUPANCY_LOOKBACK_S + 5
    )
    p._flush_buffer()
    assert sent == []


def test_a_stale_window_still_lets_happy_through():
    """Happy never consults the span, so the lookback cannot silence it."""
    p, sent = _perception(
        ["Happy"], ["Happy"], attempt_age_s=_OCCUPANCY_LOOKBACK_S + 5
    )
    p._flush_buffer()
    assert _labels(sent) == ["Happy"]


def test_a_quiet_window_emits_nothing():
    p, sent = _perception([], [_NO_READING] * 5)
    p._flush_buffer()
    assert sent == []
