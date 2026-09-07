"""Device-side pipe for product-analytics events produced by HAL.

Generic on purpose: voice KPI is the first tracker, more will follow. A
tracker builds a dict of fields and calls :func:`report`; this module owns
everything after that — the local log line, a bounded queue, one background
sender thread, and the POST to os-server (which forwards to the warehouse,
see system/tracking).

Two rules:

* :func:`report` never blocks its caller. It is called from the voice and
  audio paths, where a slow or dead uplink must cost nothing.
* Every event is logged locally BEFORE it is sent, and delivery failures are
  logged and counted too. ``journalctl -u hal | grep '\\[tracking\\]'`` is the
  device-local record; the warehouse is the copy that can be missing.
"""

import json
import logging
import queue
import threading
import uuid

import requests

logger = logging.getLogger("hal.tracking")

# os-server ingestion endpoint (loopback; same host as every other HAL→OS call).
OS_TRACKING_URL = "http://127.0.0.1:5000/api/tracking/event"

# Bounded so a dead uplink cannot grow memory without limit. Sized for a burst
# of turns, not for offline buffering: dropping and SAYING SO beats pretending.
QUEUE_SIZE = 128
POST_TIMEOUT_S = 3.0

_queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_SIZE)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

# Delivery health, attached to later events so loss is visible in the
# warehouse instead of silently inflating success rates.
_dropped = 0
_failed = 0
_counter_lock = threading.Lock()


def new_event_id() -> str:
    """A unique id for one observation, used by os-server to de-duplicate."""
    return uuid.uuid4().hex


def report(event_name: str, params: dict, event_id: str = "") -> None:
    """Queue one tracking event. Non-blocking; never raises."""
    if not event_name:
        return
    try:
        event_id = event_id or new_event_id()
        payload = {
            "event_name": event_name,
            "event_id": event_id,
            "params": _with_counters(params or {}),
        }
        # The device-local record. Must exist whether or not the POST works.
        logger.info("[tracking] %s %s", event_name, json.dumps(payload["params"], default=str))
        _ensure_worker()
        try:
            _queue.put_nowait(payload)
        except queue.Full:
            with _counter_lock:
                global _dropped
                _dropped += 1
                dropped = _dropped
            logger.warning(
                "[tracking] event dropped -- queue full (event=%s id=%s dropped_total=%d)",
                event_name, event_id, dropped,
            )
    except Exception:
        # A tracker must never take the voice path down with it.
        logger.exception("[tracking] report failed (event=%s)", event_name)


def stats() -> dict:
    """Delivery health for this process: events dropped and POSTs failed."""
    with _counter_lock:
        return {"dropped": _dropped, "failed": _failed}


def _with_counters(params: dict) -> dict:
    with _counter_lock:
        return {**params, "hal_dropped_total": _dropped, "hal_failed_total": _failed}


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="tracking-sender", daemon=True)
        _worker.start()


def _run() -> None:
    while True:
        payload = _queue.get()
        try:
            resp = requests.post(OS_TRACKING_URL, json=payload, timeout=POST_TIMEOUT_S)
            if resp.status_code != 200:
                _note_failure(payload, f"os-server returned {resp.status_code}")
        except Exception as e:  # noqa: BLE001 - transport errors are expected offline
            _note_failure(payload, str(e))


def _note_failure(payload: dict, reason: str) -> None:
    with _counter_lock:
        global _failed
        _failed += 1
        failed = _failed
    # The event is already in the log above; this line says it never left the
    # device — the part a warehouse query cannot tell you.
    logger.warning(
        "[tracking] delivery failed (event=%s id=%s failed_total=%d): %s",
        payload.get("event_name"), payload.get("event_id"), failed, reason,
    )
