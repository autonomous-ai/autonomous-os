"""stop() must wake a drain loop parked in queue.get(), not let it time out.

Both drain loops in _speak_sync only re-check the stop event between queue
gets, and the head queue's get blocks for up to 2s. A newer turn preempting
the current one waits 2.0s for the lock, so a worker that needs longer than
that to notice the stop loses the race: the old turn is cut mid-word and the
new one is refused with a 503, leaving the device silent.
"""

import queue
import threading
import time

from hal.drivers.voice.tts.service import TTSService


def _service():
    """A TTSService with only the stop/drain-queue plumbing initialized."""
    svc = object.__new__(TTSService)
    svc._speaking = True
    svc._stop_event = threading.Event()
    svc._pending_queue = []
    svc._pending_queue_lock = threading.Lock()
    svc._drain_queues = []
    svc._drain_queues_lock = threading.Lock()
    return svc


def test_stop_unblocks_a_parked_get_immediately():
    svc = _service()
    q: queue.Queue = queue.Queue(maxsize=256)
    svc._register_drain_queue(q)
    waited = {}

    def drain():
        t0 = time.monotonic()
        # The real loop's head-queue get: 2s is the window stop() must beat.
        item = q.get(timeout=2.0)
        waited["elapsed"] = time.monotonic() - t0
        waited["item"] = item

    t = threading.Thread(target=drain)
    t.start()
    time.sleep(0.05)
    svc.stop()
    t.join(timeout=2.0)

    assert not t.is_alive()
    # The sentinel is what the loops already read as end-of-stream.
    assert waited["item"] is None
    assert waited["elapsed"] < 0.5, "stop() left the drain loop waiting out its timeout"


def test_stop_wakes_every_registered_queue():
    svc = _service()
    head: queue.Queue = queue.Queue(maxsize=256)
    tail: queue.Queue = queue.Queue(maxsize=128)
    svc._register_drain_queue(head)
    svc._register_drain_queue(tail)

    svc.stop()

    assert head.get_nowait() is None
    assert tail.get_nowait() is None


def test_forgotten_queues_are_not_woken():
    # Playback drops its registrations before releasing the lock, so a stop
    # aimed at the NEXT turn cannot inject a sentinel into the finished one.
    svc = _service()
    q: queue.Queue = queue.Queue(maxsize=256)
    svc._register_drain_queue(q)
    svc._forget_drain_queues()

    svc.stop()

    assert q.empty()


def test_a_full_queue_is_not_an_error():
    # put_nowait raises Full, but a full queue is not one anybody is blocked
    # on — the loop sees the stop event on its next iteration regardless.
    svc = _service()
    q: queue.Queue = queue.Queue(maxsize=1)
    q.put_nowait(object())
    svc._register_drain_queue(q)

    svc.stop()

    assert svc._stop_event.is_set()


def test_stop_while_idle_still_clears_pending_without_waking():
    svc = _service()
    svc._speaking = False
    q: queue.Queue = queue.Queue(maxsize=256)
    svc._register_drain_queue(q)
    svc._pending_queue.append("queued sentence")

    svc.stop()

    assert svc._pending_queue == []
    assert not svc._stop_event.is_set()
    assert q.empty()
