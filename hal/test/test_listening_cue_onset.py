"""The listening cue must be visible on its first frame, not after the breath rises."""

import threading
from unittest import mock

import hal.app_state as state
from hal.drivers.rgb.effects import RGB_CMD_SOLID, breathing
from hal.presets import EMOTION_PRESETS, EMO_GREETING, EMO_LISTENING


class _RecordingService:
    """Collects dispatched frames and stops the loop after `limit` of them."""

    def __init__(self, stop_event, limit):
        self.frames = []
        self._stop = stop_event
        self._limit = limit

    def dispatch(self, cmd, color):
        assert cmd == RGB_CMD_SOLID
        self.frames.append(color)
        if len(self.frames) >= self._limit:
            self._stop.set()


def _run(color, start_at_peak, limit=3):
    stop = threading.Event()
    svc = _RecordingService(stop, limit)
    breathing(color, 100.0, None, stop, svc, start_at_peak=start_at_peak)
    return svc.frames


# The lamp's own listening colour: dim enough that int() truncation renders
# the start of the arc as literal black.
LAMP_LISTENING = (0, 0, 3)


def test_breathing_still_fades_in_by_default():
    assert _run(LAMP_LISTENING, start_at_peak=False)[0] == (0, 0, 0)


def test_start_at_peak_paints_full_brightness_on_the_first_frame():
    assert _run(LAMP_LISTENING, start_at_peak=True)[0] == LAMP_LISTENING


def test_start_at_peak_breathes_down_and_never_exceeds_the_preset():
    frames = _run((0, 0, 30), start_at_peak=True, limit=10)
    assert frames[0] == (0, 0, 30)
    assert all(f[2] <= 30 for f in frames), frames
    assert frames[-1][2] < frames[0][2], frames


def test_only_the_opening_arc_starts_at_the_peak():
    # The opening arc is the peak-to-0 half only: 50 frames. Everything after
    # it is an ordinary cycle, which rises from 0 at the usual slow pace.
    frames = _run((0, 0, 30), start_at_peak=True, limit=62)
    assert frames[49][2] == 0, frames[45:62]
    assert frames[50][2] == 0, frames[45:62]
    assert frames[61][2] > frames[51][2], frames[45:62]
    assert max(f[2] for f in frames[50:]) < frames[0][2], frames[45:62]


def test_listening_is_the_only_preset_that_opens_at_the_peak():
    assert EMOTION_PRESETS[EMO_LISTENING]["start_at_peak"] is True
    opted_in = [e for e, p in EMOTION_PRESETS.items() if p.get("start_at_peak")]
    assert opted_in == [EMO_LISTENING], opted_in
    assert "start_at_peak" not in EMOTION_PRESETS[EMO_GREETING]


def test_emotion_led_passes_the_flag_to_the_effect_thread(monkeypatch):
    monkeypatch.setattr(state, "_sleeping", False)
    monkeypatch.setattr(state, "_tts_speaking", False)
    monkeypatch.setattr(state, "rgb_service", mock.Mock())
    monkeypatch.setattr(state, "display_service", None)
    monkeypatch.setattr(state, "sensing_service", None)
    monkeypatch.setattr(state, "_stop_current_effect", lambda: None)
    started = {}

    class _Thread:
        def __init__(self, *args, **kwargs):
            started.update(kwargs.get("kwargs") or {})
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(state.threading, "Thread", _Thread)
    state._apply_emotion_led_display(EMO_LISTENING, force_led=True)
    assert started.get("start_at_peak") is True

    started.clear()
    state._apply_emotion_led_display(EMO_GREETING, force_led=True)
    assert started.get("start_at_peak") is False
