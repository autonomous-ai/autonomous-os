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
    service._on_unspoken_reply = None
    return service


def test_delayed_older_turn_is_dropped_before_it_can_rejoin_queue():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2

    assert service.speak_queue("late old reply", turn_id="old-run", turn_seq=1) is True
    assert service._latest_queue_turn_id == "new-run"
    assert service._pending_queue == []


# A dropped turn is acknowledged as success to os-server, so HAL is the only
# place that knows the reply existed and was never heard. A delegated turn has
# already had save_main_handoff record the question with "its spoken reply
# follows", so losing this leaves the realtime agent holding the placeholder.
def test_dropped_older_turn_still_reaches_the_realtime_history_hook():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2
    unspoken = []
    service._on_unspoken_reply = unspoken.append

    service.speak_queue(
        "late old reply", turn_id="old-run", turn_seq=1, realtime_feedback=True
    )

    assert unspoken == ["late old reply"]


def test_conflicting_turn_sequence_also_reports_the_unspoken_reply():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2
    unspoken = []
    service._on_unspoken_reply = unspoken.append

    service.speak_queue(
        "same seq, other run", turn_id="old-run", turn_seq=2, realtime_feedback=True
    )

    assert unspoken == ["same seq, other run"]


# Only the agentic runtime's own reply may enter the realtime model's context —
# a dropped filler or system notice must not, exactly as on the playback path.
def test_dropped_non_agent_speech_is_not_fed_to_realtime():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2
    unspoken = []
    service._on_unspoken_reply = unspoken.append

    service.speak_queue("dead-air filler", turn_id="old-run", turn_seq=1)

    assert unspoken == []


# A hook that raises must not turn an acknowledged drop into a 500.
def test_failing_hook_does_not_break_the_drop_path():
    service = _queue_service()
    service._latest_queue_turn_id = "new-run"
    service._latest_queue_turn_seq = 2

    def boom(_):
        raise RuntimeError("realtime socket is gone")

    service._on_unspoken_reply = boom

    assert service.speak_queue(
        "late old reply", turn_id="old-run", turn_seq=1, realtime_feedback=True
    ) is True


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


# --- os-server restarting its turn counter -----------------------------------
#
# The counter lives in os-server, this threshold lives in HAL, and they restart
# independently. Device-observed 03/09/2026: after an os-server deploy, seq=1
# met latest_seq=40 and the wake greeting was dropped — LED and servo ran, no
# sound — and the next 39 turns would have gone the same way.


def test_a_lower_sequence_from_a_newer_run_is_adopted_not_dropped(monkeypatch, tmp_path):
    service = _queue_service()
    service._latest_queue_turn_id = "device-chat-505-1788420332920"
    service._latest_queue_turn_seq = 40
    service._tts_cache_path = lambda _: tmp_path / "missing.wav"
    played = []
    service._speak_sync = played.append
    monkeypatch.setattr(tts_service_module.threading, "Thread", _InlineThread)

    # Same shape as the greeting that was silenced: seq 1, but created later.
    assert service.speak_queue(
        "Good morning", turn_id="device-chat-1-1788422075499", turn_seq=1
    ) is True

    assert played == ["Good morning"]
    assert service._latest_queue_turn_id == "device-chat-1-1788422075499"
    assert service._latest_queue_turn_seq == 1


def test_a_genuinely_older_run_is_still_dropped():
    service = _queue_service()
    service._latest_queue_turn_id = "device-chat-505-1788422075499"
    service._latest_queue_turn_seq = 40

    # Lower seq AND created earlier: a late POST from a superseded turn.
    assert service.speak_queue(
        "late old reply", turn_id="device-chat-1-1788420332920", turn_seq=1
    ) is True
    assert service._latest_queue_turn_seq == 40


# Channel ids carry no creation stamp, so there is nothing to compare and the
# plain sequence rule must still hold.
def test_an_id_without_a_stamp_keeps_the_sequence_rule():
    service = _queue_service()
    service._latest_queue_turn_id = "tg-991"
    service._latest_queue_turn_seq = 40

    assert service.speak_queue("late", turn_id="tg-990", turn_seq=1) is True
    assert service._latest_queue_turn_seq == 40
