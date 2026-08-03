"""Dispatch a finalized voice turn to the OS server + SER.

Extracted from VoiceService._stream_session. Identifies the speaker, routes the
turn to the OS server based on how the realtime agent resolved it, and submits
the utterance for speech-emotion recognition.
"""

import logging

from hal import config as hal_config
from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL

logger = logging.getLogger("hal.voice")


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
    decorator, sensing_sender, combined, audio_buffer, ser_audio_buffer, rt,
    identity=None,
):
    """Identify the speaker, send the turn to the OS server, and submit SER.

    Routing by the realtime outcome ``rt``:
      handled   → send as ``voice_agent_handled`` (+ input-branching hint) so the
                  main agent stays silent — realtime already spoke.
      delegated → forward the agent's instruction summary + STT transcript.
      neither   → send the plain transcript so the main agent answers.

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

    final_text, event_type = decorator.resolve_wake_word_split(combined)
    user = UNKNOWN_USER_LABEL

    if combined:
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
            sensing_sender.send(
                f"[skills: input-branching]\n[HANDLED] {final_msg}\n[REPLY] {reply}",
                event_type="voice_agent_handled",
                skip_echo=True,
            )
        elif rt.delegated:
            # Delegated — send voice agent's summary + STT transcript to the OS server
            if rt.delegate_msg:
                sensing_msg: str = f"[voice-instruction] {rt.delegate_msg}\n[transcript] {final_msg}"
            else:
                sensing_msg = final_msg
            # Hand off the just-captured frame (if any) so the agent reuses it.
            if vision_hint and sensing_msg:
                sensing_msg = f"{vision_hint}\n{sensing_msg}"
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
            sensing_sender.send(
                fallback_msg,
                event_type=event_type,
                image_b64=vision_image if vision_hint else "",
            )

    # Submit SER — uses the UNTRIMMED snapshot so laughter / sighs survive.
    decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
