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
    now = time.time()
    p._emotion_buffer = {
        PERSON: [
            EmotionData(
                frame=frame, face=face, emotion=e, confidence=0.7,
                ts=now - attempt_age_s,
            )
            for e in readings
        ]
    }
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

    Three Anger among three failures is 3 of 6 — not a majority — where the old
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


def test_even_happy_is_not_reported_from_a_stale_reading():
    """Happy's exemption is from the OCCUPANCY test, not from freshness.

    A smile from ninety seconds ago is not news now, and reporting it was the
    other half of the stale-buffer defect. Under arrival evaluation this case
    barely arises — a reading is judged in the tick it lands — but the window
    is what guarantees it.
    """
    p, sent = _perception(
        ["Happy"], ["Happy"], attempt_age_s=_OCCUPANCY_LOOKBACK_S + 5
    )
    p._flush_buffer()
    assert sent == []


def test_a_quiet_window_emits_nothing():
    p, sent = _perception([], [_NO_READING] * 5)
    p._flush_buffer()
    assert sent == []



def test_three_of_five_is_a_majority_and_survives():
    """Observed on the device: a real Surprise held 3 of 5 attempts. The first
    setting (2/3, needing 4) dropped it — three agreeing frames is evidence, and
    an expression only spans a few frames at ~2.2s between triggers.
    """
    p, sent = _perception(
        ["Surprise"] * 3, ["Surprise"] * 3 + [_NO_READING] * 2
    )
    p._flush_buffer()
    assert _labels(sent) == ["Surprise"]


def test_an_exact_tie_is_not_a_majority():
    """2 of 4 is half, not more than half. Admitting ties costs a false event."""
    p, sent = _perception(["Anger"] * 2, ["Anger"] * 2 + [_NO_READING] * 2)
    p._flush_buffer()
    assert sent == []


def test_a_lone_reading_in_a_sparse_window_is_not_enough():
    """1 of 1 is a majority but not evidence. Observed on device 2026-09-04
    12:42: a single Sad fired because the face was detected only once in the
    window — attempts are counted per face detection, not per sensing tick.
    """
    p, sent = _perception(["Sad"], ["Sad"])
    p._flush_buffer()
    assert sent == []


def test_two_of_three_still_survives():
    """The floor must not swallow a brief but real expression."""
    p, sent = _perception(["Sad"] * 2, ["Sad"] * 2 + [_NO_READING])
    p._flush_buffer()
    assert _labels(sent) == ["Sad"]


def test_a_lone_happy_is_still_exempt():
    """Happy never consults the span, so the floor cannot silence a smile."""
    p, sent = _perception(["Happy"], ["Happy"])
    p._flush_buffer()
    assert _labels(sent) == ["Happy"]


def test_a_short_expression_is_judged_while_it_is_still_fresh():
    """Observed 2026-09-04 12:55: four clean Surprise readings, then the face
    left frame. The callback that drives the decision only fires on a DETECTED
    face, so the tail sat unevaluated for 91s and was finally judged against a
    window it no longer overlapped — "Surprise held 0/1". Two readings arriving
    together must decide there and then.
    """
    p, sent = _perception(["Surprise"] * 2, ["Surprise"] * 2)
    p._flush_buffer()
    assert _labels(sent) == ["Surprise"]


def test_readings_older_than_the_window_are_not_voted_on():
    """The 0/N shape: readings kept past the span that judges them."""
    p, sent = _perception(
        ["Surprise"] * 3, [_NO_READING], attempt_age_s=_OCCUPANCY_LOOKBACK_S + 60
    )
    p._flush_buffer()
    assert sent == []


def test_only_the_person_that_fired_is_cleared():
    p, sent = _perception(["Happy"], ["Happy"])
    p._emotion_buffer["someone_else"] = list(p._emotion_buffer[PERSON])
    p._flush_buffer()
    assert _labels(sent) == ["Happy"]
    assert PERSON not in p._emotion_buffer
    assert "someone_else" in p._emotion_buffer
