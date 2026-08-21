"""Turn-ownership tests for the HAL queued TTS boundary."""

import threading

from hal.drivers.voice.tts import service as tts_service_module
from hal.drivers.voice.tts.service import TTSService


class _AvailableBackend:
    available = True


class _InlineThread:
    """Run the target synchronously so this test needs no audio device."""

    def __init__(self, target, args=(), **_):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _queue_service():
    """Small TTSService shell that exercises queue ownership only."""
    service = object.__new__(TTSService)
    service._backend = _AvailableBackend()
    service._sd = object()
    service._queue_request_lock = threading.Lock()
    service._pending_queue_lock = threading.Lock()
    service._pending_queue = []
    service._latest_queue_turn_id = ""
    service._latest_queue_turn_seq = 0
    service._stop_event = threading.Event()
    service._lock = threading.Lock()
    service._speaking = False
    service._interruptible = False
    service._last_spoken_text = ""
    service._realtime_feedback = False
    service._speaker_muted = lambda: False
    return service


def test_delayed_older_turn_is_dropped_before_it_can_rejoin_queue():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2

    assert service.speak_queue("late old reply", turn_id="old-run", turn_seq=1) is True
    assert service._latest_queue_turn_id == "new-run"
    assert service._pending_queue == []


def test_newer_turn_stops_current_speech_and_takes_the_lock(monkeypatch, tmp_path):
    service = _queue_service()
    service._latest_queue_turn_id = "old-run"
    service._latest_queue_turn_seq = 1
    service._speaking = True
    service._lock.acquire()  # Simulate the old playback worker holding it.
    service._tts_cache_path = lambda _: tmp_path / "missing.wav"

    stopped = []
    played = []

    def stop_old_turn():
        stopped.append(True)
        service._stop_event.set()
        service._speaking = False
        service._lock.release()

    def play_new_turn(text):
        played.append(text)
        service._speaking = False
        service._lock.release()

    service.stop = stop_old_turn
    service._speak_sync = play_new_turn
    monkeypatch.setattr(tts_service_module.threading, "Thread", _InlineThread)

    assert service.speak_queue("new reply", turn_id="new-run", turn_seq=2) is True
    assert stopped == [True]
    assert played == ["new reply"]
    assert service._latest_queue_turn_id == "new-run"
    assert service._latest_queue_turn_seq == 2
