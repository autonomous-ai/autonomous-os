"""Dispatch a finalized voice turn to the OS server + SER.

Extracted from VoiceService._stream_session. Identifies the speaker, routes the
turn to the OS server based on how the realtime agent resolved it, and submits
the utterance for speech-emotion recognition.
"""

import logging

from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL

logger = logging.getLogger("hal.voice")


def _take_vision_handoff() -> str:
    """Consume the frame the realtime `look` tool just captured and return a
    one-line hint pointing the main agent at it (so it reuses the image instead
    of snapshotting again), or "" if none is fresh.

    Always clears the app_state slot — the frame belongs to exactly ONE turn
    (look runs in run_realtime_turn, this runs right after in the same turn), so a
    later unrelated delegate never picks up a stale image. A freshness guard is a
    belt-and-suspenders backstop in case dispatch didn't run on the prior turn.
    """
    import os
    import time

    from hal import app_state as state
    from hal import config as cfg

    path = getattr(state, "realtime_look_frame_path", None)
    ts = getattr(state, "realtime_look_frame_ts", 0.0)
    state.realtime_look_frame_path = None
    state.realtime_look_frame_ts = 0.0
    if not path:
        return ""
    max_age = getattr(cfg, "REALTIME_GEMINI_VISION_HANDOFF_MAX_AGE_S", 20.0)
    if max_age > 0 and (time.monotonic() - ts) > max_age:
        return ""
    if not os.path.exists(path):
        return ""
    return (
        f"[vision-image] {path} (a photo was JUST captured for this request — "
        "read this file to answer the visual question; do NOT take a new snapshot)"
    )


def dispatch_turn(decorator, sensing_sender, combined, audio_buffer, ser_audio_buffer, rt):
    """Identify the speaker, send the turn to the OS server, and submit SER.

    Routing by the realtime outcome ``rt``:
      handled   → send as ``voice_agent_handled`` (+ input-branching hint) so the
                  main agent stays silent — realtime already spoke.
      delegated → forward the agent's instruction summary + STT transcript.
      neither   → send the plain transcript so the main agent answers.

    ``audio_buffer`` is the trimmed buffer (speaker recognition); ``ser_audio_buffer``
    is the untrimmed snapshot (SER keeps laughter / sighs).
    """
    # Consume the realtime `look` frame once per turn, regardless of branch below
    # (so a handled turn that already used it doesn't leak it to a later delegate).
    vision_hint = _take_vision_handoff()

    final_text, event_type = decorator.resolve_wake_word_split(combined)
    user = UNKNOWN_USER_LABEL

    if combined:
        final_msg, se_user = decorator.identify_and_decorate(final_text, audio_buffer)
        user = se_user if se_user else UNKNOWN_USER_LABEL
        logger.info("Final message → OS server (%s): %r", event_type, final_msg)

        if rt.handled:
            # Realtime already spoke — send as "voice_handled" to skip dead-air filler.
            # Include skill hint so OpenClaw reads input-branching and responds NO_REPLY.
            sensing_sender.send(
                f"[skills: input-branching]\n[HANDLED] {final_msg}\n[REPLY] {rt.transcript}",
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
                sensing_sender.send(sensing_msg, event_type=event_type)
        else:
            # Realtime not active, OR it was active but produced no output
            # (e.g. receive() timed out) — send to the OS server normally so the
            # main agent handles the turn instead of nobody answering. If a `look`
            # frame was captured this turn (Gemini died mid-vision), hand it off so
            # the agent answers from it instead of snapshotting again.
            fallback_msg = f"{vision_hint}\n{final_msg}" if vision_hint else final_msg
            sensing_sender.send(fallback_msg, event_type=event_type)

    # Submit SER — uses the UNTRIMMED snapshot so laughter / sighs survive.
    decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
