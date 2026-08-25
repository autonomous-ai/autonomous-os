"""Dispatch a finalized voice turn to the OS server + SER.

Extracted from VoiceService._stream_session. Identifies the speaker, routes the
turn to the OS server based on how the realtime agent resolved it, and submits
the utterance for speech-emotion recognition.
"""

import logging

from hal import config as hal_config
from hal.drivers.voice._internal.realtime_turn import should_drop_realtime_rejection
from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL

logger = logging.getLogger("hal.voice")


def _take_look_snapshot_marker() -> str:
    """Marker for the look frame captured this turn, or "" if there was none.

    Attached to the turn message so the Flow Monitor shows the picture in the
    SAME turn as the question and the spoken reply — the way emotion.detected
    does — instead of a bare path in a turn of its own.

    os-server strips `[snapshot: ...]` before the message reaches the model, so
    this is display-only and costs no tokens.
    """
    from hal import app_state as state

    try:
        from hal.realtime.look_monitor import snapshot_marker

        path = getattr(state, "realtime_look_monitor_path", None)
        state.realtime_look_monitor_path = None
        return snapshot_marker(path)
    except Exception:
        return ""


def _take_vision_handoff() -> tuple[str, str]:
    """Consume the frame the realtime `look` tool just captured and return
    ``(hint, image_b64)`` — a one-line hint for the main agent plus the frame
    itself as base64 — or ``("", "")`` if none is fresh.

    The frame rides the sensing POST's ``image`` field; os-server describes it
    with a vision model and forwards the description as text (describe-first —
    see system/vision in services). The path stays in the hint for
    traceability/monitoring only: telling the agent to *read* it does not work
    on a text-only main model (tool-read image blocks are silently dropped).

    Always clears the app_state slot — the frame belongs to exactly ONE turn
    (look runs in run_realtime_turn, this runs right after in the same turn), so a
    later unrelated delegate never picks up a stale image. A freshness guard is a
    belt-and-suspenders backstop in case dispatch didn't run on the prior turn.
    """
    import base64
    import os
    import time

    from hal import app_state as state
    from hal import config as cfg

    path = getattr(state, "realtime_look_frame_path", None)
    ts = getattr(state, "realtime_look_frame_ts", 0.0)
    state.realtime_look_frame_path = None
    state.realtime_look_frame_ts = 0.0
    if not path:
        return "", ""
    max_age = getattr(cfg, "REALTIME_GEMINI_VISION_HANDOFF_MAX_AGE_S", 20.0)
    if max_age > 0 and (time.monotonic() - ts) > max_age:
        return "", ""
    try:
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return "", ""
    hint = (
        f"[vision-image] {path} (a photo was JUST captured for this request; the "
        "image or its description accompanies this message — answer the visual "
        "question from it; do NOT take a new snapshot)"
    )
    return hint, image_b64


def dispatch_turn(
    decorator,
    sensing_sender,
    combined,
    audio_buffer,
    ser_audio_buffer,
    rt,
    event_type_override: str | None = None,
    identity=None,
):
    """Identify the speaker, send the turn to the OS server, and submit SER.

    Routing by the realtime outcome ``rt``:
      handled   → send as ``voice_agent_handled`` (+ input-branching hint) so the
                  main agent stays silent — realtime already spoke.
      delegated → forward the agent's instruction summary + STT transcript.
      rejected  → drop only an explicit ``reject_turn`` tool result.
      neither   → send the plain transcript so the main agent answers.

    ``rt.route`` records WHICH of those happened and why (see the ROUTE_* values
    in realtime_turn); it is logged once per turn as ``[turn] route=…``. The
    separate explicit-rejection bit is the sole policy gate that may suppress a
    normal fallback.

    ``audio_buffer`` is the trimmed buffer (speaker recognition); ``ser_audio_buffer``
    is the untrimmed snapshot (SER keeps laughter / sighs).

    ``identity`` — an optional pre-computed ``(final_msg, se_user, display)`` from
    ``decorator.identify_and_decorate``, produced by the realtime turn's speaker-ID
    prepass. When given we reuse it instead of running the (embedding-server)
    recognition a SECOND time here. None → compute it now (non-realtime path).
    """
    # Consume the realtime `look` frame once per turn, regardless of branch below
    # (so a handled turn that already used it doesn't leak it to a later delegate).
    vision_hint, vision_image = _take_vision_handoff()
    look_snap = _take_look_snapshot_marker()

    def _close_look_trace(status: str, answer: str = "", error: str = "") -> None:
        """Close the LOOK-DEBUG trace once the turn has an answer. No-op when a
        look did not happen this turn, or when tracing is off."""
        try:
            from hal.drivers.tracking import look_debug

            look_debug.finish(status, question=final_msg, answer=answer, error=error)
        except Exception:
            pass

    final_text, event_type = decorator.classify_wake_word(combined)
    if event_type_override is not None:
        event_type = event_type_override
    user = UNKNOWN_USER_LABEL

    # One line per turn saying where it went and why. Every branch below leads
    # somewhere different, but only the delegated one used to announce itself —
    # so a turn answered by the main agent because the realtime session had died
    # looked identical in the journal to one the model deliberately handed off.
    # Grep `[turn] route=` to follow any turn end to end.
    rejected = should_drop_realtime_rejection(rt)
    if rejected:
        destination = "nowhere (model explicitly rejected non-user turn)"
    elif rt.handled:
        destination = "realtime (main agent notified, stays silent)"
    elif not combined:
        destination = "nowhere (no transcript — nothing to send)"
    else:
        destination = "main agent"
    logger.info(
        "[turn] route=%s → %s (event=%s, stt=%r)",
        rt.route,
        destination,
        event_type,
        combined[:80] if combined else "(empty)",
    )

    if combined and not rejected:
        # Reuse the prepass result when the realtime path already identified the
        # speaker this turn; otherwise identify now. Never runs recognition twice.
        if identity is not None:
            final_msg, se_user, _ = identity
        else:
            final_msg, se_user, _ = decorator.identify_and_decorate(
                final_text, audio_buffer
            )
        user = se_user if se_user else UNKNOWN_USER_LABEL
        logger.info("Final message → OS server (%s): %r", event_type, final_msg)

        if rt.handled:
            # Realtime already spoke — send as "voice_handled" to skip dead-air filler.
            # Include skill hint so OpenClaw reads input-branching and responds NO_REPLY.
            # [REPLY] is capped: it exists only so the main agent's memory knows
            # what was said — the gist, not the full monologue, on a turn that
            # is pure overhead (NO_REPLY) to begin with.
            reply: str = rt.transcript or ""
            max_reply = hal_config.REALTIME_REPLY_SYNC_MAX_CHARS
            if len(reply) > max_reply:
                reply = reply[:max_reply] + " …[truncated]"
            handled_msg = f"[skills: input-branching]\n[HANDLED] {final_msg}\n[REPLY] {reply}"
            if look_snap:
                handled_msg = f"{handled_msg}\n{look_snap}"
            sensing_sender.send(
                handled_msg,
                event_type="voice_agent_handled",
                skip_echo=True,
            )
            _close_look_trace("OK_realtime_handled", answer=reply)
        elif rt.delegated:
            # Delegated — send voice agent's summary + STT transcript to the OS server
            if rt.delegate_msg:
                sensing_msg: str = f"[voice-instruction] {rt.delegate_msg}\n[transcript] {final_msg}"
            else:
                sensing_msg = final_msg
            # Hand off the just-captured frame (if any) so the agent reuses it.
            if vision_hint and sensing_msg:
                sensing_msg = f"{vision_hint}\n{sensing_msg}"
            if look_snap and sensing_msg:
                sensing_msg = f"{sensing_msg}\n{look_snap}"
            _close_look_trace("OK_delegated")
            logger.info(
                "[realtime] Delegated with message: %r%s",
                sensing_msg[:100] if sensing_msg else "",
                " (+vision-image)" if vision_hint else "",
            )
            if sensing_msg:
                sensing_sender.send(
                    sensing_msg,
                    event_type=event_type,
                    image_b64=vision_image if vision_hint else "",
                )
        else:
            # Realtime not active, OR it was active but produced no output
            # (e.g. receive() timed out) — send to the OS server normally so the
            # main agent handles the turn instead of nobody answering. If a `look`
            # frame was captured this turn (Gemini died mid-vision), hand it off so
            # the agent answers from it instead of snapshotting again.
            fallback_msg = f"{vision_hint}\n{final_msg}" if vision_hint else final_msg
            if look_snap:
                fallback_msg = f"{fallback_msg}\n{look_snap}"
            _close_look_trace("OK_fallback")
            sensing_sender.send(
                fallback_msg,
                event_type=event_type,
                image_b64=vision_image if vision_hint else "",
            )
    elif combined:
        # Keep this filter as a distinct, easily reversible gate. No silent
        # completion, timeout, or transport error reaches here — those leave
        # rt.rejected false and follow the normal fallback above.
        logger.info(
            "[turn] Explicit AI rejection filter dropped transcript before OS dispatch"
        )

    # Submit SER — uses the UNTRIMMED snapshot so laughter / sighs survive.
    decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
