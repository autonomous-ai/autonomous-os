"""Dispatch a finalized voice turn to the OS server + SER.

Extracted from VoiceService._stream_session. Identifies the speaker, routes the
turn to the OS server based on how the realtime agent resolved it, and submits
the utterance for speech-emotion recognition.
"""

import logging

from hal.drivers.voice.speech_emotion.constants import UNKNOWN_USER_LABEL

logger = logging.getLogger("hal.voice")


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
            logger.info(
                "[realtime] Delegated with message: %r",
                sensing_msg[:100] if sensing_msg else "",
            )
            if sensing_msg:
                sensing_sender.send(sensing_msg, event_type=event_type)
        else:
            # Realtime not active, OR it was active but produced no output
            # (e.g. receive() timed out) — send to the OS server normally so the
            # main agent handles the turn instead of nobody answering.
            sensing_sender.send(final_msg, event_type=event_type)

    # Submit SER — uses the UNTRIMMED snapshot so laughter / sighs survive.
    decorator.submit_speech_emotion_from_session(ser_audio_buffer, user=user)
