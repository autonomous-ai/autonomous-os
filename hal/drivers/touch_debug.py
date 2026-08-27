"""TOUCH-DEBUG: per-gesture trace files for the TTP223 edge -> session -> gesture -> action flow.

Modelled on the LOOK-DEBUG tracer in `hal/drivers/tracking/look_debug.py` — same
shape, same guarantees: OFF by default, self-contained, and it never raises. A
tracing bug must not cost the user a gesture.

Why it exists: the touch path collapses information at every layer and then
throws away the evidence. `_on_edge` receives which pad fired and whether it was
a press or a release, and uses neither. The two log lines that say what the
decision layer actually saw (`session ended (count=%d)`, `session ignored (pet
cooldown)`) are `logger.debug` and invisible at the shipped `HAL_LOG_LEVEL=INFO`.
So when a touch does the wrong thing there is no way to tell whether the pad
misfired, the session layer mis-grouped it, the classifier mis-read it, or the
action did something unexpected. This puts all four in one file per gesture.

It is also the measuring instrument for the swipe/pet question: `adjacent_deltas_ms`
and the `traversal` block are emitted for every contact whether or not swipe
classification exists, so the cross-talk-vs-stroke histogram accumulates from the
first touch without any classifier being written.

Env knobs (all optional):
  HAL_TOUCH_DEBUG              "true" to enable (OFF by default)
  HAL_TOUCH_DEBUG_DIR          output root (default: ./touch_logs next to this file)
  HAL_TOUCH_DEBUG_MAX_ENTRIES  file cap, oldest pruned (default 200; 0 = unbounded)
  HAL_TOUCH_DEBUG_PADS         line->label map, e.g. "96=S1,98=S2,100=S4". Without
                               it pads are labelled by line number, because the
                               historical S-names do not follow line order on this
                               board and guessing them would assert something false.

Layout — one file per resolved gesture, named so a wrong classification is
visible from `ls` alone:
  <root>/20260827-114032_TAP.json
  <root>/20260827-114107_PET.json
  <root>/20260827-114230_SWIPE-lr.json
  <root>/20260827-114251_IGNORED-pet_cooldown.json
  <root>/20260827-114301_IGNORED-settle.json

Deviation from look_debug: a flat .json per gesture rather than a directory. The
look tracer needs a directory to hold capture.jpg and its step frames; there are
no binary artefacts here, so a directory would just be an empty wrapper.

THREAD SAFETY IS THE POINT. `note_edge` runs inside the lgpio callback. It only
appends under a short lock — never file I/O, never a blocking call. A blocking
write there would delay subsequent edges and *manufacture* the very inter-pad
deltas this module exists to measure. The file write happens on a daemon thread
spawned by `finish`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current: Optional[Dict[str, Any]] = None
_enabled: Optional[bool] = None
_base: Optional[Path] = None
_pad_labels: Dict[int, str] = {}

# A cycle that has gone this long without resolving is flushed by the idle timer.
# Covers the startup-settle burst, which returns from `_on_edge` before any
# session timer is armed and so would otherwise never close.
FLUSH_IDLE_S: float = 2.0

# Hard cap on edges held per cycle. Continuous petting inside the pet cooldown
# can produce edges indefinitely; a debug aid must not grow without bound.
MAX_EDGES: int = 500

_flush_timer: Optional[threading.Timer] = None


def _init() -> bool:
    """Resolve config once. Returns whether tracing is on."""
    global _enabled, _base, _pad_labels
    if _enabled is not None:
        return _enabled
    _enabled = os.environ.get("HAL_TOUCH_DEBUG", "false").lower() in ("1", "true", "yes")
    if not _enabled:
        return False
    default_dir = Path(__file__).resolve().parent / "touch_logs"
    base = Path(os.environ.get("HAL_TOUCH_DEBUG_DIR", str(default_dir)))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Source tree read-only on a device deploy — fall back rather than
        # silently disabling, which would look like the feature was never on.
        import tempfile

        base = Path(tempfile.gettempdir()) / "hal-touch-debug"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("TOUCH-DEBUG disabled (no writable dir)")
            _enabled = False
            return False
        logger.warning("TOUCH-DEBUG: falling back to %s", base)
    _base = base
    _pad_labels = _parse_pad_labels(os.environ.get("HAL_TOUCH_DEBUG_PADS", ""))
    logger.info("TOUCH-DEBUG tracing ON -> %s", base)
    return True


def _parse_pad_labels(raw: str) -> Dict[int, str]:
    """Parse "96=S1,98=S2" into {96: "S1", 98: "S2"}. Malformed entries are
    skipped rather than raising — a typo in an env var must not kill touch."""
    out: Dict[int, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        line_s, _, label = part.partition("=")
        try:
            out[int(line_s.strip())] = label.strip()[:16]
        except ValueError:
            continue
    return out


def _pad(line: int) -> str:
    """Label for a line. Defaults to the line number: the board's historical
    S-names (S1/S2/S4) do not follow line order after two relocations, so
    inventing them here would assert something false. Set HAL_TOUCH_DEBUG_PADS
    once the pads are physically labelled (build-plan Phase 2.2)."""
    return _pad_labels.get(line, f"L{line}")


def _max_entries() -> int:
    try:
        return int(os.environ.get("HAL_TOUCH_DEBUG_MAX_ENTRIES", "200"))
    except ValueError:
        return 200


def _prune() -> None:
    cap = _max_entries()
    if cap <= 0 or _base is None:
        return
    try:
        files = sorted(
            (f for f in _base.iterdir() if f.is_file() and f.suffix == ".json"),
            reverse=True,
        )
        for stale in files[cap:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _arm_idle_flush() -> None:
    """(Re)arm the safety flush. Called with _lock held."""
    global _flush_timer
    if _flush_timer is not None:
        _flush_timer.cancel()
    _flush_timer = threading.Timer(FLUSH_IDLE_S, _on_idle_flush)
    _flush_timer.daemon = True
    _flush_timer.start()


def _on_idle_flush() -> None:
    """Close a cycle that went quiet without resolving to a gesture.

    Two real cases: the startup-settle burst (every edge suppressed, no session
    timer ever armed) and a cycle orphaned by an exception upstream. Naming them
    apart matters — a settle file every boot is expected, an unresolved one is a bug.
    """
    with _lock:
        trace = _current
        if trace is None:
            return
        edges = trace.get("edges") or []
        all_suppressed = bool(edges) and all(e.get("suppressed") for e in edges)
    finish("IGNORED-settle" if all_suppressed else "IGNORED-unresolved")


def start_cycle(chip: int, lines: List[int], axis: Optional[List[int]] = None) -> None:
    """Open a trace for one gesture cycle. No-op if one is already open — a
    cycle spans every edge and session from first contact to resolved action."""
    if not _init():
        return
    try:
        with _lock:
            if _current is not None:
                _arm_idle_flush()
                return
            _new_trace_locked(chip, lines, axis)
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG start_cycle failed: %s", e)


def _new_trace_locked(chip: int, lines: List[int], axis: Optional[List[int]]) -> None:
    global _current
    _current = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "_t0": time.monotonic(),
        "chip": chip,
        "lines": list(lines),
        "pads": {str(l): _pad(l) for l in lines},
        # axis = lines in physical left-to-right order. Absent until the pads
        # are physically labelled; traversal falls back to declared line order
        # and says so, because reversal detection needs SOME ordering.
        "axis": list(axis) if axis else None,
        "edges": [],
        "sessions": [],
        "_pending": [],
        "_dropped_edges": 0,
    }


def note_edge(line: int, level: int, suppressed: bool = False) -> None:
    """Record one GPIO edge. Called from the lgpio callback — append only.

    `level` is lgpio's: 0 = LOW. Pads rest HIGH (pull-up), so level 0 is the
    TOUCH edge and level 1 is the release.
    """
    if not _init():
        return
    try:
        with _lock:
            if _current is None:
                return
            if len(_current["edges"]) >= MAX_EDGES:
                _current["_dropped_edges"] += 1
                return
            rec = {
                "t_ms": round((time.monotonic() - _current["_t0"]) * 1000, 1),
                "line": line,
                "pad": _pad(line),
                "level": level,
                "suppressed": suppressed,
            }
            _current["edges"].append(rec)
            if not suppressed:
                _current["_pending"].append(rec)
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_edge failed: %s", e)


def note_session_end(count: int) -> None:
    """Close off the edges seen since the last boundary into one session.

    A session is one physical contact: the burst of cross-talk and FastMode
    auto-release edges from a single finger press. `count` is the driver's
    running session counter, recorded as-is so the trace can be compared against
    the driver's own view.
    """
    if not _init():
        return
    try:
        with _lock:
            if _current is None:
                return
            pending = _current["_pending"]
            _current["_pending"] = []
            _current["sessions"].append(_summarise_session(pending, count, _current))
            _arm_idle_flush()
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_session_end failed: %s", e)


def _summarise_session(edges: List[Dict[str, Any]], count: int,
                       trace: Dict[str, Any]) -> Dict[str, Any]:
    """Per-contact arithmetic. This is the Phase 2 measurement.

    `first_touch_order` uses only TOUCH edges (level 0), one per line: the
    release edge is FastMode's auto-drop, not a second contact, so including it
    would double-count and destroy the deltas.
    """
    touches: Dict[int, float] = {}
    for e in edges:
        if e["level"] == 0 and e["line"] not in touches:
            touches[e["line"]] = e["t_ms"]
    order = sorted(touches.items(), key=lambda kv: kv[1])
    times = [t for _, t in order]
    deltas = [round(b - a, 1) for a, b in zip(times, times[1:])]
    return {
        "n": count,
        "t_ms": edges[0]["t_ms"] if edges else None,
        "ended_t_ms": round((time.monotonic() - trace["_t0"]) * 1000, 1),
        "edge_count": len(edges),
        "pads_touched": [_pad(l) for l, _ in order],
        "first_touch_order": [[_pad(l), t] for l, t in order],
        "adjacent_deltas_ms": deltas,
        "span_ms": round(times[-1] - times[0], 1) if len(times) > 1 else 0.0,
        # First line to fire in this contact — the proxy for "where the finger
        # was". Its reliability under cross-talk is the open assumption the
        # traversal model rests on, and what this field exists to measure.
        "primary_pad": _pad(order[0][0]) if order else None,
    }


def _traversal(trace: Dict[str, Any]) -> Dict[str, Any]:
    """Pad sequence across the whole cycle, and its reversal count.

    Reversal is what separates a swipe from a stroke: a swipe is one monotonic
    pass, a pet turns around at least once. That needs an ORDERING of the pads,
    which is `axis`. Until the pads are physically labelled there is no measured
    order, so this falls back to declared line order and records which it used —
    a reversal count against an assumed axis is still useful evidence, but the
    reader must know it is provisional.

    The sequence is built from EVERY pad touched, in time order, concatenated
    across sessions — not one entry per session. Measured on orange-lamp
    2026-08-27: a stroke collapses into one or two sessions while cross-talk from
    a single finger already reaches all three pads (41% of contacts), so a
    per-session sequence never reached three distinct pads and `reversals` came
    back None on all 30 traces. The spatial information lives inside the session,
    in `first_touch_order`, so that is what this reads.

    Consecutive repeats are collapsed: a pad re-firing without an intervening
    different pad is FastMode re-triggering under a stationary finger, not a
    move, and counting it would invent direction changes that never happened.
    """
    seq: List[List[Any]] = []
    for s in trace["sessions"]:
        for pad, t in s.get("first_touch_order") or []:
            if seq and seq[-1][0] == pad:
                continue
            seq.append([pad, t])

    axis = trace.get("axis") or trace["lines"]
    axis_source = "boards.json" if trace.get("axis") else "assumed line order"
    pos_of = {_pad(l): i for i, l in enumerate(axis)}
    positions = [pos_of.get(p) for p, _ in seq]

    known = [p for p in positions if p is not None]
    reversals: Optional[int] = None
    monotonic: Optional[bool] = None
    if len(known) >= 3:
        deltas = [b - a for a, b in zip(known, known[1:]) if b != a]
        reversals = sum(
            1 for a, b in zip(deltas, deltas[1:]) if (a > 0) != (b > 0)
        )
        monotonic = reversals == 0

    distinct = len({p for p, _ in seq})
    if distinct == 0:
        # No contact ever formed — every edge was suppressed by the settle
        # guard, or the cycle was flushed before a session closed. Distinct
        # from "one pad", which means a finger really did land.
        verdict = "no contacts recorded"
    elif distinct < 2:
        verdict = "no traversal — contact stayed on one pad"
    elif reversals is None:
        verdict = f"traversal over {distinct} pads, too few steps to judge reversal"
    elif monotonic:
        verdict = f"monotonic over {distinct} pads (swipe-shaped, axis={axis_source})"
    else:
        verdict = f"{reversals} reversal(s) over {distinct} pads (stroke-shaped, axis={axis_source})"

    return {
        "pad_sequence": seq,
        "axis_positions": positions,
        "axis_source": axis_source,
        "distinct_pads": distinct,
        "steps": len(seq),
        "reversals": reversals,
        "monotonic": monotonic,
        "verdict": verdict,
    }


def note_decision(gesture: str, reason: str, session_count: int) -> None:
    """Record which gesture the driver resolved to, and why."""
    if not _init():
        return
    try:
        with _lock:
            if _current is not None:
                _current["decision"] = {
                    "session_count": session_count,
                    "gesture": gesture,
                    "reason": reason,
                }
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_decision failed: %s", e)


def note_action(fn: str, source: str, **fields: Any) -> None:
    """Record the action dispatched and the device state it ran against.

    State matters because several outcomes are state-dependent and silent
    today: a tap on a sleeping device, a gesture blocked by the hardware mic
    switch, a pet whose phrase was dropped because the speaker was muted.
    """
    if not _init():
        return
    try:
        state_snapshot = _read_state()
        with _lock:
            if _current is not None:
                _current["action"] = {
                    "fn": fn,
                    "source": source,
                    "device_state_at_dispatch": state_snapshot,
                    **fields,
                }
    except Exception as e:
        logger.debug("TOUCH-DEBUG note_action failed: %s", e)


def _read_state() -> Dict[str, Any]:
    """Snapshot the flags the touch actions branch on. Imported lazily and
    defensively — app_state pulls in most of HAL, and a debug aid must not be
    the reason a driver fails to import."""
    try:
        import hal.app_state as state

        return {
            "sleeping": getattr(state, "_sleeping", None),
            "mic_muted": getattr(state, "_mic_muted", None),
            "speaker_muted": getattr(state, "_speaker_muted", None),
            "hw_mic_switch": getattr(state, "_hw_mic_switch_muted", None),
            "enrolling": getattr(state, "_enrolling", None),
        }
    except Exception:
        return {}


def finish(status: str) -> None:
    """Close the cycle and write it out. `status` becomes the filename suffix,
    so a wrong classification is visible from `ls` alone."""
    global _current, _flush_timer
    if not _init():
        return
    try:
        with _lock:
            trace = _current
            _current = None
            if _flush_timer is not None:
                _flush_timer.cancel()
                _flush_timer = None
        if trace is None or _base is None:
            return
        # Any edges not yet closed into a session still belong in the record —
        # a cycle that resolved mid-contact is exactly the interesting case.
        if trace["_pending"]:
            trace["sessions"].append(
                _summarise_session(trace["_pending"], -1, trace)
            )
        trace["_pending"] = []
        trace["traversal"] = _traversal(trace)
        trace["total_ms"] = round((time.monotonic() - trace.pop("_t0")) * 1000, 1)
        dropped = trace.pop("_dropped_edges", 0)
        if dropped:
            trace["edges_dropped"] = dropped
        trace.pop("_pending", None)
        _log_summary(status, trace)
        # Off-thread: json.dump + fsync must never sit on a Timer thread that
        # the driver still needs, and must never reach the lgpio callback path.
        threading.Thread(
            target=_write, args=(status, trace), daemon=True, name="touch-debug-write"
        ).start()
    except Exception as e:
        logger.debug("TOUCH-DEBUG finish failed: %s", e)


def _write(status: str, trace: Dict[str, Any]) -> None:
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in status)[:40]
        path = _base / f"{stamp}_{safe}.json"
        # Same-second gestures would collide; petting produces exactly that.
        if path.exists():
            n = 2
            while (_base / f"{stamp}_{safe}-{n}.json").exists():
                n += 1
            path = _base / f"{stamp}_{safe}-{n}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
        _prune()
    except Exception as e:
        logger.debug("TOUCH-DEBUG write failed: %s", e)


def _log_summary(status: str, trace: Dict[str, Any]) -> None:
    """One INFO line accounting for the whole gesture.

    INFO deliberately: the driver's own decision lines are logger.debug and
    never appear at the shipped HAL_LOG_LEVEL, which is the gap this closes.
    """
    try:
        tv = trace.get("traversal") or {}
        pads = sorted({e["pad"] for e in trace["edges"] if not e["suppressed"]})
        seq = [p for p, _ in tv.get("pad_sequence", [])]
        spans = [s["span_ms"] for s in trace["sessions"] if s.get("span_ms")]
        action = (trace.get("action") or {}).get("fn", "-")
        rev = tv.get("reversals")
        logger.info(
            "TOUCH-TRACE %s pads=%s edges=%d sessions=%d span=%sms seq=%s rev=%s "
            "-> %s (resolved +%.0fms)",
            status, pads, len(trace["edges"]), len(trace["sessions"]),
            max(spans) if spans else 0, seq,
            "?" if rev is None else rev, action, trace["total_ms"],
        )
    except Exception:
        pass
