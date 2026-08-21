"""Pipecat turn handling — the cascaded twin of realtime_turn.py.

Same contract (drive one turn, return a RealtimeTurnResult) over a different
input: the finished STT transcript instead of committed audio. There is no
native-audio path, no look-replay and no WS-recovery retry — a cascaded brain
has no session to lose, and an empty transcript means there is simply nothing
to send.

The wait filler, thinking cue and CoT leak filter are shared with realtime_turn
so both brains feel identical to the user.
"""

import json
import logging
import threading
from typing import Callable

from hal import config as hal_config
from hal.pipecat_rt import TextChunk, ToolCall
from hal.realtime.orchestrator import (
    DEFAULT_EMOTION_INTENSITY,
    DELEGATE_TOOL_NAME,
    EMOTION_TOOL_NAME,
    RealtimeOrchestrator,
)
from hal.drivers.voice._internal.cot_leak_filter import CoTLeakFilter, clean_transcript
from hal.drivers.voice._internal.realtime_turn import (
    SENTENCE_ENDS,
    RealtimeTurnResult,
    _reply_language_name,
    _thinking_cue_clear,
    _thinking_cue_start,
    _WaitFiller,
)

logger = logging.getLogger("hal.voice")


def _fire_emotion(arguments: str) -> None:
    """Run express_emotion off-thread so the reply never waits on the face."""
    try:
        args: dict = json.loads(arguments) if arguments else {}
    except (ValueError, TypeError):
        return
    emotion: str = str(args.get("emotion", "")).strip().lower()
    if not emotion:
        return
    intensity: float = DEFAULT_EMOTION_INTENSITY
    try:
        intensity = max(0.0, min(1.0, float(args.get("intensity", intensity))))
    except (ValueError, TypeError):
        pass
    threading.Thread(
        target=RealtimeOrchestrator._fire_emotion,
        args=(emotion, intensity),
        daemon=True,
    ).start()


def run_pipecat_turn(
    session,
    tts,
    strip_markers: Callable[[str], str],
    combined: str,
) -> RealtimeTurnResult:
    """Send the transcript to the pipecat brain and speak its reply.

    Returns how the turn resolved so the caller can forward (delegate),
    suppress (handled), or fall back to the main agent.
    """
    if not hal_config.REALTIME_ENABLED:
        return RealtimeTurnResult()
    if not session.available:
        logger.warning("[pipecat] enabled but not available — falling back to OS server")
        return RealtimeTurnResult()

    text: str = (combined or "").strip()
    if not text:
        logger.info("[pipecat] empty transcript — nothing to send (no audio path)")
        return RealtimeTurnResult()

    delegated = False
    handled = False
    delegate_msg = ""
    text_parts: list[str] = []
    sentence_buf = ""
    first_sentence_sent = False
    reply_lang: str = _reply_language_name()
    leak_filter = CoTLeakFilter(reply_lang)
    wait_filler = _WaitFiller()

    _thinking_cue_start()
    wait_filler.arm()
    try:
        for event in session.run_turn(text):
            if isinstance(event, ToolCall):
                if event.name == DELEGATE_TOOL_NAME:
                    delegated = True
                    try:
                        delegate_msg = str(
                            json.loads(event.arguments or "{}").get("message", "")
                        )
                    except (ValueError, TypeError):
                        delegate_msg = ""
                    # The main-agent hop that follows fires its own filler.
                    wait_filler.cancel()
                    session.tool_result(
                        event.call_id, '{"status": "delegated"}', run_llm=False
                    )
                    continue
                if event.name == EMOTION_TOOL_NAME:
                    _fire_emotion(event.arguments)
                    # run_llm=True: the face is a side effect, the reply still owes
                    # the user words.
                    session.tool_result(event.call_id, '{"status": "ok"}', run_llm=True)
                    continue
                logger.warning("[pipecat] unknown tool %r", event.name)
                session.tool_result(
                    event.call_id, '{"error": "unknown tool"}', run_llm=True
                )
                continue

            if delegated or not isinstance(event, TextChunk):
                continue

            text_parts.append(event.text)
            sentence_buf += event.text
            if tts is not None and sentence_buf.rstrip().endswith(SENTENCE_ENDS):
                sentence: str = leak_filter.filter_text(strip_markers(sentence_buf))
                if sentence:
                    if not first_sentence_sent:
                        logger.info("[pipecat] First sentence → speak: %r", sentence[:80])
                        wait_filler.cancel()
                        if not tts.speak(sentence):
                            tts.speak_queue(sentence)
                        first_sentence_sent = True
                        _thinking_cue_clear()
                    else:
                        logger.info(
                            "[pipecat] Next sentence → speak_queue: %r", sentence[:80]
                        )
                        tts.speak_queue(sentence)
                sentence_buf = ""

        transcript: str = clean_transcript(
            strip_markers("".join(text_parts)), reply_lang
        )

        if delegated:
            logger.info("[pipecat] Delegated → will forward to OS server")
        else:
            remaining: str = leak_filter.filter_text(strip_markers(sentence_buf))
            if remaining and tts is not None:
                if not first_sentence_sent:
                    logger.info("[pipecat] Final fragment → speak: %r", remaining[:80])
                    wait_filler.cancel()
                    if not tts.speak(remaining):
                        tts.speak_queue(remaining)
                    first_sentence_sent = True
                    _thinking_cue_clear()
                else:
                    logger.info(
                        "[pipecat] Final fragment → speak_queue: %r", remaining[:80]
                    )
                    tts.speak_queue(remaining)
            # Same rule as the audio-native path: only claim the turn when the
            # device actually spoke, so an empty reply still reaches the main agent.
            if first_sentence_sent or transcript:
                handled = True
                logger.info(
                    "[pipecat] Chit-chat complete — agent_reply=%r",
                    transcript[:200] if transcript else "(empty)",
                )
                session.save_turn(
                    user_text=text, agent_text=transcript or "(empty)"
                )
            else:
                logger.info("[pipecat] No output (empty / timeout) — falling back")
                _thinking_cue_clear()
                try:
                    from hal.routes.led import restore_led

                    restore_led()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[pipecat] Processing failed: %s — will forward to OS server", e)
        _thinking_cue_clear()
        try:
            session.abort_turn()
        except Exception:
            pass
        return RealtimeTurnResult(delegated=True)
    finally:
        wait_filler.cancel()
        try:
            session.turn_finished()
        except Exception:
            logger.exception("[pipecat] turn bookkeeping failed")

    return RealtimeTurnResult(delegated, handled, transcript, delegate_msg)
