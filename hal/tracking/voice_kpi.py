"""Voice KPI measurement — observations only, no behaviour change.

Two questions, measured on the device because HAL is the only process that
knows when audio actually reached the speaker:

  KPI-1  Was the user's voice command acknowledged within
         ``ACK_DEADLINE_MS`` of the detected end of their speech?
  KPI-2  Did an outdated reply actually get played after a suppression
         boundary (an explicit stop, or the realtime agent answering a newer
         question)?

Design notes that matter for reading the numbers:

* **Playback, not acceptance.** The ack signal is the first audio FRAME
  (``TTSService._on_speak_start``), never an HTTP 200, a queued sentence, or
  model output arriving. ``speak_queue`` deliberately returns True for
  requests it drops, so acceptance proves nothing.
* **Monotonic, one process.** Every elapsed time here is a difference of two
  ``time.monotonic()`` reads taken inside HAL. os-server's clock is never
  subtracted from HAL's.
* **A detected endpoint, not the acoustic truth.** ``speech_end_method``
  records HOW the end of speech was decided (silence clock, STT final, mic
  closed by TTS/music). The KPI is "after we decided the user stopped", and
  the field is carried so a later analysis can correct for it.
* **Suppression is not failure.** A reply correctly withheld is recorded as
  an outcome, not as a missing response. Excluded interactions are still
  reported, with the reason, so nothing eligible is silently discarded.

Everything is emitted through :mod:`hal.tracking.client`; nothing here blocks
the voice path, and nothing here changes what the device says.
"""

import logging
import threading
import time

from hal.tracking import client

logger = logging.getLogger("hal.tracking")

# --- KPI thresholds ---------------------------------------------------------
# The target ack window. Recorded latency is kept raw, so this number can move
# without re-instrumenting anything.
ACK_DEADLINE_MS = 3000
# Keep watching past the target so a LATE ack is reported with its real
# latency instead of collapsing into "no ack at all".
ACK_OBSERVE_WINDOW_MS = 10000

# Playback does not stop the instant a boundary is stamped: TTSService.stop()
# sets an event and wakes the drain loops, and the worker still has to release
# the lock (device-observed on lamp-0c89: up to ~4s in the pathological case
# the wake-the-queues fix was written for; sub-second normally). Audio still
# heard inside this grace is NOT counted as stale — but the raw
# stop_to_silence_ms is recorded on every boundary, so this threshold is a
# reporting choice that can be re-decided from the data, not a hidden pass.
STALE_GRACE_MS = 2000
# How long after the grace to keep watching for old audio starting or
# continuing before closing the observation.
STALE_OBSERVE_MS = 3000

# Event names in the warehouse.
EVENT_INTERACTION = "voice_kpi_interaction"
EVENT_SUPPRESSION = "voice_kpi_suppression"

# --- Playback kinds (what was heard) ---------------------------------------
KIND_AGENT_REPLY = "agent_reply"        # main agent's answer, via the TTS queue
KIND_NATIVE_REALTIME = "native_realtime"  # realtime model's own voice
KIND_WAITING_AUDIO = "waiting_audio"    # filler / "one moment"
KIND_SYSTEM_AUDIO = "system_audio"      # OS notice, cached phrase, greeting

# Acknowledgement modality per kind — the user-perceivable receipt signal.
# All of these are AUDIO. Visual-only feedback (the listening LED) is NOT
# counted: instrumenting its real activation is a separate change, and
# labelling it as an ack without measuring it would overstate the KPI.
_ACK_MODALITY = {
    KIND_AGENT_REPLY: "spoken_answer",
    KIND_NATIVE_REALTIME: "spoken_answer_realtime",
    KIND_WAITING_AUDIO: "waiting_audio",
    KIND_SYSTEM_AUDIO: "acknowledgement_audio",
}

# --- Outcomes ---------------------------------------------------------------
OUTCOME_ACKED = "acknowledged"          # audio played (may be slower than target)
OUTCOME_NO_ACK = "no_ack"               # eligible, nothing was ever heard
OUTCOME_EXCLUDED = "excluded"           # not a KPI sample; see exclusion_reason

# Exclusions — every one of these is REPORTED, never dropped silently.
EXCL_REJECTED_NOISE = "rejected_noise"
EXCL_REJECTED_NON_USER = "rejected_non_user"
EXCL_NO_TRANSCRIPT = "no_transcript"
EXCL_NOT_ADDRESSED = "not_addressed"    # no wake word / outside follow-up window
EXCL_SPEAKER_MUTED = "speaker_muted"
EXCL_INTERRUPTED = "interrupted_by_user"

# Suppression boundary reasons.
BOUNDARY_EXPLICIT_STOP = "explicit_stop"      # user clicked; different semantics
BOUNDARY_AUTO_SUPERSEDE = "auto_supersede"    # realtime answered a newer turn

_MAX_TRACKED = 32


class _Interaction:
    """One user utterance, from detected speech end to its KPI-1 verdict."""

    __slots__ = (
        "id", "speech_end", "speech_end_method", "event_type", "route",
        "run_id", "ack_latency_ms", "ack_modality", "ack_kind",
        "exclusion_reason", "reported", "timer",
    )

    def __init__(self, iid: str, speech_end: float, method: str):
        self.id = iid
        self.speech_end = speech_end
        self.speech_end_method = method
        self.event_type = ""
        self.route = ""
        self.run_id = ""
        self.ack_latency_ms = None
        self.ack_modality = ""
        self.ack_kind = ""
        self.exclusion_reason = ""
        self.reported = False
        self.timer = None


_lock = threading.RLock()
_interactions: "dict[str, _Interaction]" = {}
_order: "list[str]" = []
_by_run: "dict[str, str]" = {}
# The utterance currently coming out of the speaker, or None.
_playing = None  # dict(kind, interaction_id, started, turn_id)


def _now() -> float:
    return time.monotonic()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


# --- Capture boundary -------------------------------------------------------

def speech_end(method: str) -> str:
    """Register the detected end of a user utterance. Returns its id.

    ``method`` says HOW the endpoint was detected — ``silence_clock``,
    ``stt_final``, ``tts_started``, ``music_started``, ``max_duration``,
    ``stt_error``.
    """
    iid = "vi-" + client.new_event_id()[:16]
    with _lock:
        it = _Interaction(iid, _now(), method)
        _interactions[iid] = it
        _order.append(iid)
        while len(_order) > _MAX_TRACKED:
            _forget(_order[0])
        it.timer = threading.Timer(ACK_OBSERVE_WINDOW_MS / 1000.0, _close_interaction, args=(iid,))
        it.timer.daemon = True
        it.timer.start()
    logger.info("[voice-kpi] speech end (interaction=%s method=%s)", iid, method)
    return iid


def set_route(iid: str, route: str, event_type: str = "") -> None:
    """Record how the turn was routed (realtime handled / delegated / main
    agent / dropped). Free-form; it comes straight from the dispatcher."""
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.route = route
        if event_type:
            it.event_type = event_type


def bind_run(iid: str, run_id: str) -> None:
    """Attach the os-server run id, so the reply that comes back through the
    TTS queue can be attributed to the utterance that asked for it."""
    if not iid or not run_id:
        return
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.run_id = run_id
        _by_run[run_id] = iid


def exclude(iid: str, reason: str) -> None:
    """Mark an interaction as not a KPI sample, with a reason. Reported, not
    discarded — a KPI whose exclusions are invisible cannot be audited."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.exclusion_reason:
            return
        it.exclusion_reason = reason
    logger.info("[voice-kpi] excluded (interaction=%s reason=%s)", iid, reason)


# --- Playback boundary ------------------------------------------------------

def playback_start_from_tts(tts) -> None:
    """Classify what the speaker just started playing and record the ack.

    Reads only public TTSService state, so the classification rule lives here
    with the rest of the measurement instead of being spread through the audio
    code:

      native voice          → the realtime model answering in its own voice
      realtime_feedback     → the main agent's reply (the only speech fed back
                              to the realtime session), carrying its turn id
      interruptible cached  → a filler, i.e. waiting audio
      anything else         → system audio (notices, greetings, cached phrases)
    """
    try:
        if getattr(tts, "native_mode", False):
            kind, turn_id = KIND_NATIVE_REALTIME, ""
        elif getattr(tts, "realtime_feedback", False):
            kind, turn_id = KIND_AGENT_REPLY, getattr(tts, "latest_queue_turn_id", "")
        elif getattr(tts, "interruptible", False):
            kind, turn_id = KIND_WAITING_AUDIO, ""
        else:
            kind, turn_id = KIND_SYSTEM_AUDIO, ""
        playback_start(kind, turn_id)
    except Exception:
        logger.exception("[voice-kpi] playback_start_from_tts failed")



def playback_start(kind: str, turn_id: str = "") -> None:
    """The speaker just produced its first audio frame for ``kind``.

    Attribution: an agent reply carries the os-server run id it was queued
    for; realtime voice, fillers and system audio belong to the newest
    utterance still waiting for an answer.
    """
    started = _now()
    with _lock:
        iid = _by_run.get(turn_id, "") if turn_id else ""
        if not iid:
            iid = _newest_open_interaction()
        global _playing
        _playing = {"kind": kind, "interaction_id": iid, "started": started, "turn_id": turn_id}
        it = _interactions.get(iid) if iid else None
        if it is not None and it.ack_latency_ms is None:
            it.ack_latency_ms = _ms(started - it.speech_end)
            it.ack_kind = kind
            it.ack_modality = _ACK_MODALITY.get(kind, kind)
            logger.info(
                "[voice-kpi] ack (interaction=%s kind=%s latency_ms=%d)",
                iid, kind, it.ack_latency_ms,
            )
        # Same audio, second question: is this a superseded turn talking?
        if iid:
            _observe_playback_for_boundaries(kind, iid, started)


def playback_end() -> None:
    """Playback finished or was interrupted."""
    ended = _now()
    with _lock:
        global _playing
        was = _playing
        _playing = None
        if not was:
            return
        # How long old audio kept going after a boundary. Recorded raw on the
        # boundary event whether or not it exceeded the grace, so the grace
        # itself stays auditable.
        for state in _watchers:
            if was["interaction_id"] in state["applicable"] and state["stop_to_silence_ms"] is None:
                state["stop_to_silence_ms"] = _ms(ended - state["at"])


# --- Suppression boundary (KPI-2) ------------------------------------------

def boundary(reason: str, triggering_interaction_id: str = "") -> None:
    """Stamp a suppression boundary and start watching for stale audio.

    ``reason`` is BOUNDARY_EXPLICIT_STOP (the user said stop) or
    BOUNDARY_AUTO_SUPERSEDE (the realtime agent answered a newer utterance).
    They are different policies and are reported separately — automatic
    supersession is gated by os-server's OS_REALTIME_SUPERSEDES_MAIN_REPLY,
    the explicit stop is not.
    """
    at = _now()
    with _lock:
        applicable = _interactions_before(at, triggering_interaction_id)
        playing = dict(_playing) if _playing else None
    state = {
        "reason": reason,
        "at": at,
        "event_id": client.new_event_id(),
        "trigger_interaction_id": triggering_interaction_id,
        "applicable": applicable,
        "stale_observed": False,
        "stale_kind": "",
        "stale_started_after_ms": None,
        "stop_to_silence_ms": None,
        "old_audio_playing_at_boundary": bool(
            playing and playing["interaction_id"] in applicable
        ),
    }
    _watchers.append(state)
    logger.info(
        "[voice-kpi] suppression boundary (reason=%s applicable=%d playing_old=%s)",
        reason, len(applicable), state["old_audio_playing_at_boundary"],
    )
    t = threading.Timer(
        (STALE_GRACE_MS + STALE_OBSERVE_MS) / 1000.0, _close_boundary, args=(state,)
    )
    t.daemon = True
    t.start()


_watchers: "list[dict]" = []


def _interactions_before(at: float, trigger_id: str) -> "set[str]":
    """Which interactions the boundary applies to.

    Explicit stop covers everything in flight. Automatic supersession covers
    only what is OLDER than the utterance that triggered it — a newer
    utterance is exactly what the policy is protecting.
    """
    trigger = _interactions.get(trigger_id)
    cutoff = trigger.speech_end if trigger is not None else at
    return {
        iid for iid, it in _interactions.items()
        if it.speech_end < cutoff and iid != trigger_id
    }


def _observe_playback_for_boundaries(kind: str, iid: str, started: float) -> None:
    """Called from playback_start: does this audio belong to a superseded
    interaction, and did it start after the boundary + grace?"""
    for state in list(_watchers):
        if state["stale_observed"] or iid not in state["applicable"]:
            continue
        after_ms = _ms(started - state["at"])
        if after_ms >= STALE_GRACE_MS:
            state["stale_observed"] = True
            state["stale_kind"] = kind
            state["stale_started_after_ms"] = after_ms
            logger.warning(
                "[voice-kpi] STALE playback started %dms after %s boundary (kind=%s interaction=%s)",
                after_ms, state["reason"], kind, iid,
            )


def _close_boundary(state: dict) -> None:
    """Close one boundary observation and report it (KPI-2 denominator)."""
    with _lock:
        playing = dict(_playing) if _playing else None
        if (
            not state["stale_observed"]
            and playing
            and playing["interaction_id"] in state["applicable"]
        ):
            # Old audio is STILL coming out, well past the grace.
            state["stale_observed"] = True
            state["stale_kind"] = playing["kind"]
            state["stale_started_after_ms"] = _ms(playing["started"] - state["at"])
        try:
            _watchers.remove(state)
        except ValueError:
            pass

    client.report(
        EVENT_SUPPRESSION,
        {
            "interaction_id": state["trigger_interaction_id"],
            "suppression_reason": state["reason"],
            "applicable_interactions": len(state["applicable"]),
            "old_audio_playing_at_boundary": state["old_audio_playing_at_boundary"],
            "stale_observed": state["stale_observed"],
            "stale_kind": state["stale_kind"],
            "stale_started_after_ms": state["stale_started_after_ms"],
            "stop_to_silence_ms": state["stop_to_silence_ms"],
            "grace_ms": STALE_GRACE_MS,
            "observe_window_ms": STALE_OBSERVE_MS,
        },
        event_id=state["event_id"],
    )


# --- Reporting --------------------------------------------------------------

def _newest_open_interaction() -> str:
    for iid in reversed(_order):
        it = _interactions.get(iid)
        if it is not None and not it.reported:
            return iid
    return ""


def _forget(iid: str) -> None:
    it = _interactions.pop(iid, None)
    if iid in _order:
        _order.remove(iid)
    if it is not None:
        if it.run_id:
            _by_run.pop(it.run_id, None)
        if it.timer is not None:
            it.timer.cancel()


def _close_interaction(iid: str) -> None:
    """Report one interaction (KPI-1 sample) once its observation window is up."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.reported:
            return
        if it.ack_latency_ms is None and not it.exclusion_reason and _speaker_muted():
            # Nothing was heard because the speaker is off. That is a muted
            # device, not a missed response.
            it.exclusion_reason = EXCL_SPEAKER_MUTED
        it.reported = True
        params = _interaction_params(it)
    client.report(EVENT_INTERACTION, params, event_id="int-" + iid)


def _speaker_muted() -> bool:
    """Whether the device speaker is muted right now (single mute gate lives
    in app_state; lazy import keeps this module importable on its own)."""
    try:
        from hal import app_state

        return bool(app_state._speaker_muted)
    except Exception:
        return False


def _interaction_params(it: "_Interaction") -> dict:
    eligible = not it.exclusion_reason
    if it.exclusion_reason:
        outcome = OUTCOME_EXCLUDED
    elif it.ack_latency_ms is not None:
        outcome = OUTCOME_ACKED
    else:
        outcome = OUTCOME_NO_ACK
    return {
        "interaction_id": it.id,
        "run_id": it.run_id,
        "event_type": it.event_type,
        "route": it.route,
        "speech_end_method": it.speech_end_method,
        "eligible": eligible,
        "outcome": outcome,
        "exclusion_reason": it.exclusion_reason,
        # Raw observation: kept whatever the verdict, so the threshold can
        # change without re-instrumenting the device.
        "ack_latency_ms": it.ack_latency_ms,
        "ack_modality": it.ack_modality,
        "ack_kind": it.ack_kind,
        "ack_deadline_ms": ACK_DEADLINE_MS,
        "observe_window_ms": ACK_OBSERVE_WINDOW_MS,
    }


def reset_for_test() -> None:
    """Clear all state. Tests only."""
    with _lock:
        for iid in list(_order):
            _forget(iid)
        _interactions.clear()
        _order.clear()
        _by_run.clear()
        for state in list(_watchers):
            _watchers.remove(state)
        global _playing
        _playing = None
