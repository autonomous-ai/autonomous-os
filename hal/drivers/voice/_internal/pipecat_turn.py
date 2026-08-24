"""Pipecat turn handling — the cascaded twin of realtime_turn.py.

Same contract (drive one turn, return a RealtimeTurnResult) over a different
input: the finished STT transcript instead of committed audio. There is no
native-audio path, no look-replay and no WS-recovery retry — a cascaded brain
has no session to lose, and an empty transcript means there is simply nothing
to send.

The wait filler, thinking cue and CoT leak filter are shared with realtime_turn
so both brains feel identical to the user.
"""

import contextlib
import json
import logging
import re
import threading
from typing import Callable

from hal import config as hal_config
from hal.pipecat_rt import TextChunk as _PipecatText, ToolCall as _PipecatTool
from hal.realtime.voice_agent.openai_cascaded import (
    TextChunk as _CascadedText,
    ToolCall as _CascadedTool,
)
from hal.realtime.orchestrator import (
    DEFAULT_EMOTION_INTENSITY,
    DELEGATE_TOOL_NAME,
    EMOTION_TOOL_NAME,
    LOOK_TOOL_NAME,
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

# Sent instead of a transcript when the user cut the device off but their words
# did not survive. They are talking over the loudspeaker, and cancellation on
# this hardware is nearly all nonlinear suppression, which removes the near-end
# along with the echo (measured WER 1.00 on real double talk). Going silent is
# the worst possible response to being interrupted, so hand the floor back.
INTERRUPTED_NO_TEXT = (
    "[The user just interrupted you mid-sentence, but their words could not be "
    "made out. Stop what you were saying, acknowledge the interruption in one "
    "short sentence, and ask what they need.]"
)
INTERRUPTED_PREFIX = "[The user interrupted you mid-sentence:] "

# The two cascaded brains emit their own event classes with identical shape, so
# the driver matches on either. Neither import pulls in pipecat itself.
TEXT_CHUNK = (_PipecatText, _CascadedText)
TOOL_CALL = (_PipecatTool, _CascadedTool)


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


# Defense in depth: prompting alone (system_prompt_pipecat.md) did not stop a
# model from sometimes writing a tool call as TEXT instead of a real function
# call (device-observed on qwen/qwen3.6-35b-a3b, 2026-08-24: agent_reply =
# '[express_emotion] {"emotion": "greeting"} I\'m here! ...'). Cascaded has no
# native audio — every character left in a reply is spoken verbatim by TTS —
# so a leaked marker like this gets read aloud, JSON and all, unless caught
# here. Named tools only (not a bare `[word] {...}`): a legitimate reply could
# coincidentally contain a bracketed word followed by a JSON-shaped clause.
_LEAKED_TOOL_CALL_RE = re.compile(
    r"\[\s*(express_emotion|delegate_to_main|look|web_search)\s*\]\s*(\{[^}]*\})?",
    re.IGNORECASE,
)


def _repair_leaked_tool_calls(text: str, fire: bool = False) -> str:
    """Repair a leaked `[tool_name] {...}` marker in `text`.

    `express_emotion` is REFORMATTED, not deleted: `[express_emotion]
    {"emotion": "greeting"}` becomes `[greeting]` — the same square-bracket
    delivery-tag convention ElevenLabs v3 already uses for `[laughs]`,
    `[calm]`, etc. (see system_prompt_pipecat.md's Allowed Audio Tags). Those
    tags are delivery cues, not spoken words — ElevenLabs shapes the reading
    from them rather than reading them aloud — so this fixes the leak AND
    keeps the emotional colour, instead of throwing it away.

    `fire=True` additionally fires the real express_emotion side effect (the
    face/LED) via the same _fire_emotion() a correct tool call uses — the
    model's intent lands twice: once as a delivery cue in speech, once
    physically. Call with fire=True exactly once per leaked marker — at the
    site that actually speaks the text — so it doesn't double-fire when the
    same raw text is ALSO cleaned for the saved transcript.

    The other three tool names have no natural delivery-tag equivalent —
    their arguments carry real intent (a message, a capture, a query) that
    isn't safe to reconstruct from malformed text — so those are stripped
    entirely, same as an express_emotion leak with no parseable emotion.
    """
    if "[" not in text:
        return text

    def _handle(m: "re.Match[str]") -> str:
        name, args = m.group(1).lower(), m.group(2)
        if name == "express_emotion" and args:
            try:
                emotion = str(json.loads(args).get("emotion", "")).strip().lower()
            except (ValueError, TypeError):
                emotion = ""
            if emotion:
                if fire:
                    logger.warning(
                        "[pipecat] recovered a leaked express_emotion call: %s", args
                    )
                    _fire_emotion(args)
                else:
                    logger.warning(
                        "[pipecat] reformatted a leaked express_emotion call: %s", args
                    )
                return f"[{emotion}]"
        logger.warning("[pipecat] stripped a leaked %s call from the reply", name)
        return ""

    cleaned = _LEAKED_TOOL_CALL_RE.sub(_handle, text)
    return re.sub(r"  +", " ", cleaned).strip()


def _capture_look_frame():
    """Aim, capture and persist one camera frame for the `look` tool.

    Reuses RealtimeOrchestrator's capture path instead of reimplementing it —
    same pattern as _fire_emotion above. Persisting the frame also arms the
    existing Flow Monitor snapshot marker and, where look_debug exists,
    closes its trace via the SAME mechanism turn_dispatch.dispatch_turn
    already uses for the audio-native path — no closing call needed here.

    Aim and tracing are OPTIONAL: `hal.drivers.tracking.aim` / `look_debug`
    and the `LOOK_AIM_*` config knobs are newer additions some deployed HAL
    builds predate, so both degrade to a plain, untraced capture rather than
    failing the tool call. `RealtimeOrchestrator._capture_frame` is called
    with no positional args for the same reason — it takes an optional
    `settle_s` on the current codebase but older builds accept none at all;
    omitting it works against both.

    Returns the BGR frame, or None if unavailable.
    """
    try:
        from hal.drivers.tracking import look_debug
    except ImportError:
        look_debug = None
    if look_debug is not None:
        look_debug.start()

    try:
        from hal.drivers.tracking.aim import servo_ownership
    except ImportError:
        servo_ownership = contextlib.nullcontext

    with servo_ownership():
        if getattr(hal_config, "LOOK_AIM_ENABLED", False):
            try:
                from hal.drivers.tracking.aim import aim_for_look

                res = aim_for_look(hal_config.LOOK_AIM_DEADLINE_S)
                if look_debug is not None:
                    look_debug.note_aim(res)
            except Exception as e:
                logger.warning("[pipecat] look: aim raised, capturing anyway: %s", e)
        frame = RealtimeOrchestrator._capture_frame()

    if frame is None:
        logger.warning("[pipecat] look: no camera frame available")
        if look_debug is not None:
            try:
                look_debug.abandon("no_camera_frame")
            except Exception:
                pass
        return None
    RealtimeOrchestrator._persist_look_frame(frame)
    return frame


def run_pipecat_turn(
    session,
    tts,
    strip_markers: Callable[[str], str],
    combined: str,
    cancelled: Callable[[], bool] | None = None,
    interrupted: bool = False,
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

    user_text: str = (combined or "").strip()
    text: str = user_text
    if interrupted:
        # The barge-in gate already proved someone spoke: two consecutive
        # frames over the level threshold AND a speech classifier agreeing.
        # Whether the words survived cancellation is a separate question.
        text = f"{INTERRUPTED_PREFIX}{text}" if text else INTERRUPTED_NO_TEXT
        logger.info(
            "[pipecat] turn follows a barge-in (transcript=%r)",
            user_text or "(empty)",
        )
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
    stopped = False

    def _cancelled() -> bool:
        return cancelled is not None and cancelled()

    try:
        for event in session.run_turn(text):
            if _cancelled():
                # Barge-in. Drop the rest of the reply rather than queueing it
                # behind the user's new question.
                stopped = True
                logger.info("[pipecat] barge-in — abandoning the rest of this reply")
                try:
                    session.abort_turn()
                except Exception:
                    pass
                break
            if isinstance(event, TOOL_CALL):
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
                if event.name == LOOK_TOOL_NAME:
                    frame = _capture_look_frame()
                    if frame is None:
                        session.tool_result(
                            event.call_id, '{"error": "camera unavailable"}', run_llm=True
                        )
                    else:
                        # run_llm=True: the model still owes an answer — the
                        # image rides alongside, CascadedAgent adds it to
                        # context so the very next round already sees it.
                        session.tool_result(
                            event.call_id,
                            '{"result": "captured — describe what you currently see"}',
                            run_llm=True,
                            image=frame,
                        )
                    continue
                logger.warning("[pipecat] unknown tool %r", event.name)
                session.tool_result(
                    event.call_id, '{"error": "unknown tool"}', run_llm=True
                )
                continue

            if delegated or not isinstance(event, TEXT_CHUNK):
                continue

            text_parts.append(event.text)
            sentence_buf += event.text
            if tts is not None and sentence_buf.rstrip().endswith(SENTENCE_ENDS):
                sentence: str = leak_filter.filter_text(
                    strip_markers(_repair_leaked_tool_calls(sentence_buf, fire=True))
                )
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

        # fire=False: the same leaked marker was already recovered/fired at the
        # speaking sites above — this only cleans the SAVED transcript text.
        transcript: str = clean_transcript(
            strip_markers(_repair_leaked_tool_calls("".join(text_parts))), reply_lang
        )

        if delegated:
            logger.info("[pipecat] Delegated → will forward to OS server")
        else:
            remaining: str = leak_filter.filter_text(
                strip_markers(_repair_leaked_tool_calls(sentence_buf, fire=True))
            )
            if remaining and tts is not None and not stopped:
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
                    "[pipecat] %s — agent_reply=%r",
                    "Interrupted mid-reply" if stopped else "Chit-chat complete",
                    transcript[:200] if transcript else "(empty)",
                )
                # Store what the user actually said, not the wrapper: the
                # marker is guidance for this reply, not conversation history.
                session.save_turn(
                    user_text=user_text or "(interrupted — words not intelligible)",
                    agent_text=transcript or "(empty)",
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
