"""Voice KPI measurement — observations only, no behaviour change.

Two questions, measured on the device because HAL is the only process that
knows when audio actually reached the speaker:

  KPI-1  Was the user's voice command acknowledged within
         ``ACK_DEADLINE_MS`` of the detected end of their speech?
  KPI-2  Did an outdated reply actually get played after a suppression
         boundary (an explicit stop, or the realtime agent answering a newer
         question)?

Both targets (3 s / ≥95 % / <1 %) are **provisional**: every raw duration is
stored, so thresholds can be re-decided from the data instead of by
re-instrumenting devices.

Design rules that decide whether a number is trustworthy:

* **Playback, not acceptance.** The ack signal is the first frame actually
  written to the audio stream (``TTSService._note_audio_written``), never an
  HTTP 200, a queued sentence, model output, or ``on_speak_start`` — the
  cached path fires that one *before* taking the stream lock, so speech that
  is stopped or fails before its first write would look played. Even the first
  write is not the acoustic onset: ALSA/Bluetooth buffering sits after it.
* **Explicit ownership.** Every playback carries the owner that claimed the
  speaker (``run:<turn_id>`` or ``interaction:<id>``). Audio with **no** owner
  is recorded as ``unknown`` and never counted as an acknowledgement — a
  guess is how an old filler gets credited to a new command.
* **Monotonic, one process.** Every elapsed time is a difference of two
  ``time.monotonic()`` reads inside HAL. os-server's clock is never
  subtracted from HAL's.
* **A detected endpoint, not the acoustic truth.** ``speech_end_method``
  records how the endpoint was decided, and the clock starts at that
  detection — before transcript assembly, not after it.
* **Incomplete is not success.** A suppression whose window closes while the
  suppressed turns are still alive reports ``observation_complete = false``,
  not "no stale reply".
"""

import logging
import threading
import time

from hal.tracking import client

logger = logging.getLogger("hal.tracking")

# --- KPI thresholds (provisional) -------------------------------------------
ACK_DEADLINE_MS = 3000
# Keep watching past the target so a LATE ack is reported with its real
# latency instead of collapsing into "no ack at all".
ACK_OBSERVE_WINDOW_MS = 10000

# Playback does not stop the instant a boundary is stamped: TTSService.stop()
# sets an event and wakes the drain loops, and the worker still has to release
# the lock (device-observed on lamp-0c89: up to ~4s in the pathological case
# the wake-the-queues fix was written for; sub-second normally). Audio inside
# this grace is not counted as stale — but stop_to_silence_ms and every
# playback interval are recorded raw, so the choice stays auditable.
STALE_GRACE_MS = 2000
# How long a suppression is watched. A main-agent reply can arrive tens of
# seconds after the boundary, so this is deliberately long; when it expires
# with suppressed turns still alive the event says so instead of passing.
SUPPRESSION_OBSERVE_MS = 60000

# How long a turn is assumed to still be ABLE to speak. Separate from the
# acknowledgement window on purpose: reporting the KPI-1 verdict after 10s
# does not mean the main agent finished — it can answer much later, and a
# stop pressed at second 12 must still find the turn alive. The clock is
# refreshed by any playback that turn produces, so a turn that keeps talking
# keeps counting as active.
TURN_ACTIVE_TTL_MS = 45000

EVENT_INTERACTION = "voice_kpi_interaction"
EVENT_SUPPRESSION = "voice_kpi_suppression"

# --- Playback kinds (what was heard) ---------------------------------------
KIND_AGENT_REPLY = "agent_reply"          # main agent's answer, via the TTS queue
KIND_NATIVE_REALTIME = "native_realtime"  # realtime model's own voice
KIND_WAITING_AUDIO = "waiting_audio"      # filler / "one moment"
KIND_SYSTEM_AUDIO = "system_audio"        # OS notice, cached phrase, greeting
KIND_UNKNOWN = "unknown"                  # nobody claimed this playback

# Acknowledgement modality per kind — the user-perceivable receipt signal.
# All AUDIO. Visual feedback (the listening LED) is NOT counted: its real
# activation is not instrumented, and counting it unmeasured would overstate
# KPI-1.
_ACK_MODALITY = {
    KIND_AGENT_REPLY: "spoken_answer",
    KIND_NATIVE_REALTIME: "spoken_answer_realtime",
    KIND_WAITING_AUDIO: "waiting_audio",
    KIND_SYSTEM_AUDIO: "acknowledgement_audio",
}

# --- Outcomes ---------------------------------------------------------------
OUTCOME_ACKED = "acknowledged"
OUTCOME_NO_ACK = "no_ack"
OUTCOME_EXCLUDED = "excluded"

EXCL_REJECTED_NOISE = "rejected_noise"
EXCL_REJECTED_NON_USER = "rejected_non_user"
EXCL_NO_TRANSCRIPT = "no_transcript"
EXCL_NOT_ADDRESSED = "not_addressed"
EXCL_SPEAKER_MUTED = "speaker_muted"
EXCL_INTERRUPTED = "interrupted_by_user"
# NOT an exclusion: the request was valid, the device simply failed to serve
# it. Recorded as a failure reason on an ELIGIBLE interaction — dropping it
# would inflate the success rate with exactly the cases that hurt the user.
FAIL_DISPATCH_FAILED = "dispatch_failed"

BOUNDARY_EXPLICIT_STOP = "explicit_stop"
BOUNDARY_AUTO_SUPERSEDE = "auto_supersede"

_MAX_TRACKED = 32


class _Interaction:
    __slots__ = (
        "id", "speech_end", "speech_end_method", "event_type", "route",
        "run_id", "ack_latency_ms", "ack_modality", "ack_kind",
        "exclusion_reason", "failure_reason", "reported", "report_event_id",
        "timer", "life_timer", "closed", "last_activity",
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
        # A failure that is NOT an exclusion: the request was valid and went
        # unserved. Stays in the KPI denominator (see _interaction_params).
        self.failure_reason = ""
        self.reported = False
        self.report_event_id = ""
        self.timer = None       # KPI-1 verdict deadline
        self.life_timer = None  # turn-lifetime deadline
        # `closed` means "can no longer speak" — NOT "already reported".
        # Reporting the acknowledgement verdict must not retire a turn the
        # main agent is still working on, or a later stop finds nothing to
        # suppress.
        self.closed = False
        self.last_activity = speech_end


_lock = threading.RLock()
_interactions: "dict[str, _Interaction]" = {}
_order: "list[str]" = []
_by_run: "dict[str, str]" = {}
_watchers: "list[dict]" = []
# The playback currently on the speaker: dict(kind, owner, interaction_id,
# started, ended). One at a time — the TTS service holds a single lock.
_playing = None
_unknown_owner_playbacks = 0


def _now() -> float:
    return time.monotonic()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


# --- Capture boundary -------------------------------------------------------

def speech_end(method: str, at: float = 0.0) -> str:
    """Register the detected end of a user utterance. Returns its id.

    ``at`` is the monotonic timestamp of the detection itself (when the
    silence clock fired / the session was cut). Pass it so transcript
    assembly, trimming and speaker-ID do not inflate every latency. Defaults
    to now for callers that have no earlier stamp.

    ``method``: ``silence_clock``, ``stt_final``, ``tts_started``,
    ``music_started``, ``max_duration``, ``stt_error``.
    """
    iid = "vi-" + client.new_event_id()[:16]
    with _lock:
        it = _Interaction(iid, at or _now(), method)
        _interactions[iid] = it
        _order.append(iid)
        while len(_order) > _MAX_TRACKED:
            _forget(_order[0])
        it.timer = threading.Timer(ACK_OBSERVE_WINDOW_MS / 1000.0, _close_interaction, args=(iid,))
        it.timer.daemon = True
        it.timer.start()
        _arm_lifetime(it)
    logger.info("[voice-kpi] speech end (interaction=%s method=%s)", iid, method)
    return iid


def _arm_lifetime(it: "_Interaction") -> None:
    """(Re)start the turn-lifetime clock. Any playback the turn produces is
    proof it is still alive, so the clock restarts from that moment."""
    if it.life_timer is not None:
        it.life_timer.cancel()
    it.life_timer = threading.Timer(TURN_ACTIVE_TTL_MS / 1000.0, _retire_interaction, args=(it.id,))
    it.life_timer.daemon = True
    it.life_timer.start()


def _retire_interaction(iid: str) -> None:
    """The turn has been silent for TURN_ACTIVE_TTL_MS: assume it can no
    longer speak. Only then does it leave the suppression denominator."""
    with _lock:
        it = _interactions.get(iid)
        if it is not None:
            it.closed = True


def set_route(iid: str, route: str, event_type: str = "") -> None:
    """Record how the turn was routed. Late arrivals amend an already-reported
    verdict rather than being silently lost (see _amend)."""
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.route = route
        if event_type:
            it.event_type = event_type
        if it.reported:
            _amend(it, "late_route")


def bind_run(iid: str, run_id: str) -> None:
    """Attach the os-server run id so the reply that comes back through the
    TTS queue is attributed to the utterance that asked for it."""
    if not iid or not run_id:
        return
    with _lock:
        it = _interactions.get(iid)
        if it is None:
            return
        it.run_id = run_id
        _by_run[run_id] = iid


def mark_failed(iid: str, reason: str) -> None:
    """Record that this interaction could not be served (the POST to
    os-server never landed, a backend error, a timeout).

    Deliberately NOT an exclusion: the user asked a valid question and got
    nothing. It stays eligible, so the KPI counts it as the failure it is.
    """
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.failure_reason:
            return
        it.failure_reason = reason
        # Nothing will answer it, so it can no longer produce stale speech.
        it.closed = True
        if it.reported:
            _amend(it, "late_failure")
            return
    logger.warning("[voice-kpi] interaction unserved (interaction=%s reason=%s)", iid, reason)


def exclude(iid: str, reason: str) -> None:
    """Mark an interaction as not a KPI sample, with a reason. Reported, never
    silently discarded. An exclusion that arrives after the verdict was sent
    emits an amendment."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.exclusion_reason:
            return
        it.exclusion_reason = reason
        it.closed = True
        if it.reported:
            _amend(it, "late_exclusion")
            return
    logger.info("[voice-kpi] excluded (interaction=%s reason=%s)", iid, reason)


# --- Playback boundary ------------------------------------------------------

def playback_audio(owner: str, kind_hint: str, tts=None) -> None:
    """The first frame of a playback actually reached the audio stream.

    ``owner`` is what claimed the speaker: ``run:<turn_id>`` (agent reply or a
    filler armed for that turn), ``interaction:<id>`` (realtime native voice),
    or "" when nobody claimed it. An unclaimed playback is recorded as
    ``unknown`` and NEVER counts as an acknowledgement.
    """
    started = _now()
    with _lock:
        iid = _owner_interaction(owner)
        kind = _classify(owner, kind_hint, tts)
        global _playing, _unknown_owner_playbacks
        _playing = {
            "kind": kind, "owner": owner, "interaction_id": iid,
            "started": started, "ended": None,
        }
        if not iid:
            _unknown_owner_playbacks += 1
            logger.info(
                "[voice-kpi] playback with unknown owner (kind=%s) -- not counted as ack",
                kind,
            )
        else:
            it = _interactions.get(iid)
            if it is not None:
                # Proof the turn is still alive: restart its lifetime clock so
                # a stop pressed later still sees something to suppress.
                it.last_activity = started
                it.closed = False
                _arm_lifetime(it)
            if it is not None and it.ack_latency_ms is None:
                it.ack_latency_ms = _ms(started - it.speech_end)
                it.ack_kind = kind
                it.ack_modality = _ACK_MODALITY.get(kind, kind)
                logger.info(
                    "[voice-kpi] ack (interaction=%s kind=%s latency_ms=%d)",
                    iid, kind, it.ack_latency_ms,
                )
        _observe_playback(kind, iid, started, None)


def playback_end() -> None:
    """Playback finished or was interrupted."""
    ended = _now()
    with _lock:
        global _playing
        was = _playing
        _playing = None
        if not was:
            return
        was["ended"] = ended
        _observe_playback(was["kind"], was["interaction_id"], was["started"], ended)


def _owner_interaction(owner: str) -> str:
    """Resolve the owner tag to an interaction.

    ``run:<id>`` normally carries an os-server run id, but the realtime paths
    (its wait filler, and text replies spoken before the turn ever reaches
    os-server) have no run id yet and tag themselves with the interaction id
    directly. Both are accepted; anything unresolvable stays unknown.
    """
    if not owner:
        return ""
    if owner.startswith("run:"):
        value = owner[4:]
        return _by_run.get(value) or (value if value in _interactions else "")
    if owner.startswith("interaction:"):
        iid = owner[len("interaction:"):]
        return iid if iid in _interactions else ""
    return ""


def _classify(owner: str, kind_hint: str, tts) -> str:
    """What the user heard. The hint comes from the write site; TTS state
    separates an agent reply from a filler on the shared cached path."""
    if kind_hint == "native_realtime":
        return KIND_NATIVE_REALTIME
    if not owner:
        return KIND_UNKNOWN
    try:
        if tts is not None and getattr(tts, "realtime_feedback", False):
            return KIND_AGENT_REPLY
        if kind_hint == "cached" and tts is not None and getattr(tts, "interruptible", False):
            return KIND_WAITING_AUDIO
    except Exception:
        pass
    return KIND_AGENT_REPLY if kind_hint == "agent_or_system" else KIND_SYSTEM_AUDIO


# --- Suppression boundary (KPI-2) ------------------------------------------

def boundary(reason: str, triggering_interaction_id: str = "", policy_applied: bool = True) -> None:
    """Stamp a suppression boundary and watch for stale audio.

    ``policy_applied`` must be the OS's ANSWER, not an assumption: automatic
    supersession only happens when os-server has
    ``OS_REALTIME_SUPERSEDES_MAIN_REPLY`` enabled and actually stamped its
    watermark. A boundary that was never applied is not a KPI-2 situation and
    is not recorded — counting it would inflate the denominator with
    situations where nothing was ever suppressed.
    """
    if not policy_applied:
        logger.info("[voice-kpi] boundary skipped -- policy not applied (reason=%s)", reason)
        return
    at = _now()
    with _lock:
        applicable = _active_interactions_before(at, triggering_interaction_id)
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
            "stale_audible_past_grace_ms": None,
            "stop_to_silence_ms": None,
            "old_audio_playing_at_boundary": bool(
                playing and playing["interaction_id"] in applicable
            ),
        }
        _watchers.append(state)
        if playing and playing["interaction_id"] in applicable:
            # Interval already in progress: re-evaluate it against this new
            # boundary so audio that merely CONTINUES past the grace counts.
            _observe_playback(playing["kind"], playing["interaction_id"],
                              playing["started"], None)
    logger.info(
        "[voice-kpi] suppression boundary (reason=%s applicable=%d playing_old=%s)",
        reason, len(applicable), state["old_audio_playing_at_boundary"],
    )
    t = threading.Timer(SUPPRESSION_OBSERVE_MS / 1000.0, _close_boundary, args=(state,))
    t.daemon = True
    t.start()


def _active_interactions_before(at: float, trigger_id: str) -> "set[str]":
    """Which ACTIVE interactions the boundary applies to.

    Only turns that can still speak count: an interaction that was excluded or
    already closed cannot produce a stale reply, so keeping it in the
    denominator would dilute KPI-2 with situations that never existed.
    Explicit stop covers everything in flight; automatic supersession covers
    only what is older than the utterance that triggered it.
    """
    trigger = _interactions.get(trigger_id)
    if trigger is None:
        # Explicit stop: everything still able to speak, including an
        # utterance that just ended (the user can say "stop" the moment they
        # finish talking).
        return {iid for iid, it in _interactions.items()
                if it.speech_end <= at and not it.closed}
    return {
        iid for iid, it in _interactions.items()
        if it.speech_end < trigger.speech_end and iid != trigger_id and not it.closed
    }


def _observe_playback(kind: str, iid: str, started: float, ended) -> None:
    """Score one playback interval against every open boundary.

    Stale means audio of a suppressed turn was audible AFTER the boundary plus
    the grace — whether it STARTED then, or merely kept going. Scoring the
    interval (not just its start) is what catches audio that began before the
    boundary and ran past the grace.
    """
    if not iid:
        return
    for state in _watchers:
        if iid not in state["applicable"]:
            continue
        limit = state["at"] + STALE_GRACE_MS / 1000.0
        stop = ended if ended is not None else _now()
        if ended is not None and state["stop_to_silence_ms"] is None:
            state["stop_to_silence_ms"] = _ms(ended - state["at"])
        if stop <= limit:
            continue  # went quiet inside the documented grace
        audible_past = _ms(stop - limit)
        if state["stale_observed"] and (state["stale_audible_past_grace_ms"] or 0) >= audible_past:
            continue
        state["stale_observed"] = True
        state["stale_kind"] = kind
        state["stale_started_after_ms"] = _ms(started - state["at"])
        state["stale_audible_past_grace_ms"] = audible_past
        logger.warning(
            "[voice-kpi] STALE playback past %s boundary "
            "(kind=%s interaction=%s started_after_ms=%d audible_past_grace_ms=%d)",
            state["reason"], kind, iid, state["stale_started_after_ms"], audible_past,
        )


def _close_boundary(state: dict) -> None:
    """Close one boundary observation and report it (KPI-2 denominator)."""
    with _lock:
        playing = dict(_playing) if _playing else None
        if playing and playing["interaction_id"] in state["applicable"]:
            _observe_playback(playing["kind"], playing["interaction_id"],
                              playing["started"], None)
        # Honest verdict: if a suppressed turn can still speak when the window
        # closes, "no stale reply" has not been established.
        still_active = [
            iid for iid in state["applicable"]
            if iid in _interactions and not _interactions[iid].closed
        ]
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
            "stale_audible_past_grace_ms": state["stale_audible_past_grace_ms"],
            "stop_to_silence_ms": state["stop_to_silence_ms"],
            "grace_ms": STALE_GRACE_MS,
            "observe_window_ms": SUPPRESSION_OBSERVE_MS,
            # False = the window closed with suppressed turns still able to
            # speak. Such a row is NOT evidence of a clean suppression.
            "observation_complete": not still_active,
            "unobserved_interactions": len(still_active),
        },
        event_id=state["event_id"],
    )


# --- Reporting --------------------------------------------------------------

def _forget(iid: str) -> None:
    it = _interactions.pop(iid, None)
    if iid in _order:
        _order.remove(iid)
    if it is not None:
        if it.run_id:
            _by_run.pop(it.run_id, None)
        if it.timer is not None:
            it.timer.cancel()
        if it.life_timer is not None:
            it.life_timer.cancel()


def _speaker_muted() -> bool:
    """Whether the device speaker is muted right now (single mute gate lives
    in app_state; lazy import keeps this module importable on its own)."""
    try:
        from hal import app_state

        return bool(app_state._speaker_muted)
    except Exception:
        return False


def _close_interaction(iid: str) -> None:
    """Report one interaction (KPI-1 sample) once its observation window is up."""
    with _lock:
        it = _interactions.get(iid)
        if it is None or it.reported:
            return
        if it.ack_latency_ms is None and not it.exclusion_reason and _speaker_muted():
            # Nothing was heard because the speaker is off. A muted device,
            # not a missed response.
            it.exclusion_reason = EXCL_SPEAKER_MUTED
        it.reported = True
        it.report_event_id = "int-" + iid
        params = _interaction_params(it)
    client.report(EVENT_INTERACTION, params, event_id="int-" + iid)


def _amend(it: "_Interaction", why: str) -> None:
    """Emit a corrected verdict for an interaction whose routing or exclusion
    arrived after its row was already sent. The warehouse keeps both; the
    amendment wins (see the docs' KPI queries)."""
    params = _interaction_params(it)
    params["amends_event_id"] = it.report_event_id
    params["amendment_reason"] = why
    logger.info("[voice-kpi] amending reported verdict (interaction=%s why=%s)", it.id, why)
    client.report(EVENT_INTERACTION, params, event_id="amend-" + client.new_event_id()[:12])


def _interaction_params(it: "_Interaction") -> dict:
    # A failure reason does NOT remove eligibility: the command was valid and
    # was not served. Only an exclusion (noise, not addressed, muted, …) means
    # "this was never a KPI sample".
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
        "failure_reason": it.failure_reason,
        "event_type": it.event_type,
        "route": it.route,
        "speech_end_method": it.speech_end_method,
        "eligible": eligible,
        "outcome": outcome,
        "exclusion_reason": it.exclusion_reason,
        # Raw observation, kept whatever the verdict, so the (provisional)
        # threshold can change without re-instrumenting the device.
        "ack_latency_ms": it.ack_latency_ms,
        "ack_modality": it.ack_modality,
        "ack_kind": it.ack_kind,
        "ack_deadline_ms": ACK_DEADLINE_MS,
        "observe_window_ms": ACK_OBSERVE_WINDOW_MS,
        # Coverage: playbacks nobody claimed. A climbing count means some
        # audio could not be attributed and was excluded from ack decisions.
        "unknown_owner_playbacks": _unknown_owner_playbacks,
        "amends_event_id": "",
        "amendment_reason": "",
    }


def reset_for_test() -> None:
    """Clear all state. Tests only."""
    with _lock:
        for iid in list(_order):
            _forget(iid)
        _interactions.clear()
        _order.clear()
        _by_run.clear()
        _watchers.clear()
        global _playing, _unknown_owner_playbacks
        _playing = None
        _unknown_owner_playbacks = 0
