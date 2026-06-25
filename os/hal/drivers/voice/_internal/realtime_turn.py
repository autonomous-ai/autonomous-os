"""Realtime agent turn handling — extracted from VoiceService._stream_session.

Given the audio already streamed to the realtime model for one speaking turn,
drive the turn (commit → stream output → speak sentences → delegate/handle) and
report the outcome. Pure helper: it touches no VoiceService state beyond the
orchestrator / TTS handles and the marker-stripper passed in.
"""

import logging
from typing import Callable, NamedTuple

from hal import app_state as hal_app_state
from hal import config as hal_config
from hal.clock import device_now
from hal.drivers.realtime.models import AudioOutput as RTAudioOutput
from hal.drivers.realtime.models import TextOutput as RTTextOutput
from hal.drivers.realtime.models.signal import DelegateSignal

logger = logging.getLogger("hal.voice")

SENTENCE_ENDS = (".", "!", "?", "。", "！", "？")


def _reply_language_name() -> str:
    """Resolve the device's reply language to a human name (e.g. "Vietnamese").

    Reads stt_language from config.json and maps it via the same table the system
    prompt uses, so the per-turn reminder and the prompt stay in sync. Returns ""
    when no language is configured — we don't force a default, mirroring the
    prompt's `_load_language() or "English"` only when one is actually set.
    """
    from hal.config import _os_cfg_get
    from hal.drivers.realtime.context_manager.base import ContextManagerBase

    code: str = (_os_cfg_get("stt_language", "") or "").strip()
    if not code:
        return ""
    return ContextManagerBase.LANGUAGE_NAMES.get(code, code)


class RealtimeTurnResult(NamedTuple):
    """Outcome of a realtime turn, consumed by the OS-server dispatch step."""

    delegated: bool = False
    handled: bool = False
    transcript: str = ""
    delegate_msg: str = ""


def run_realtime_turn(
    realtime,
    tts,
    strip_markers: Callable[[str], str],
    combined: str,
    rt_audio_buffer: list,
    buf_duration: float,
    audio_is_speech: bool = True,
) -> RealtimeTurnResult:
    """Commit the captured audio to the realtime agent and stream its reply.

    Runs even if the STT transcript is empty — the model has the raw audio.
    Speaks complete sentences as they arrive. Returns how the turn resolved so
    the caller can forward (delegate), suppress (handled), or fall back.
    """
    delegated = False
    handled = False
    transcript = ""
    delegate_msg = ""
    native = hal_config.REALTIME_NATIVE_AUDIO and tts is not None
    native_started = False  # cleanup guard: True between begin and end
    native_played = False    # did native audio actually play this turn (for handled)

    # Noise/false-trigger guard: a session with no STT transcript is not worth a
    # model turn — committing it makes the model answer silence/noise (spurious
    # self-talk + wasted tokens), which then desyncs onto a later real turn.
    # Two signals catch it: (1) a sliver of audio (just the VAD pre-roll, below
    # the duration floor), or (2) Silero VAD judged the FULL buffer non-speech
    # (catches sustained noise that runs LONGER than the floor — `audio_is_speech`
    # is computed by the caller, default True so a missing check never drops a
    # turn). A genuine audio-only turn (real speech STT missed) clears both.
    noise_turn = not combined and (
        buf_duration < hal_config.REALTIME_MIN_COMMIT_DURATION_S
        or not audio_is_speech
    )
    if (
        hal_config.REALTIME_ENABLED
        and realtime.available
        and rt_audio_buffer
        and not noise_turn
    ):
        logger.info(
            "[realtime] Entering realtime flow — committing audio (stt=%r)",
            combined[:100] if combined else "(empty)",
        )
        try:
            # Inject per-turn context before committing
            turn_ctx: list[str] = [
                f"Time: {device_now().strftime('%Y-%m-%d %H:%M:%S %A')}",
            ]
            # Per-turn language reminder. The system prompt already locks the
            # language, but Google Search grounding pulls English source text into
            # context and can drag the spoken reply into English. A reminder right
            # next to the turn (closest to generation) is the strongest lever.
            lang_name: str = _reply_language_name()
            if lang_name:
                turn_ctx.append(
                    f"Reply language: {lang_name} (answer ONLY in {lang_name}, "
                    "even if a search result or any context is in another language)"
                )
            try:
                if hal_app_state.sensing_service:
                    cu: str = (
                        hal_app_state.sensing_service._perception_orchestrator.current_user
                        or ""
                    )
                    if cu:
                        turn_ctx.append(f"Current user: {cu}")
            except Exception:
                pass
            realtime.send_text("[TURN CONTEXT] " + " | ".join(turn_ctx))

            # Drop any output still queued from a previous turn so this turn only
            # reads its OWN response. Provider replies arrive async and can lag the
            # local-VAD cadence; without this, a noise blip reads a stale prior
            # reply in milliseconds and speaks it ("Moon talks on its own" + double
            # TTS).
            realtime.flush_output()
            realtime.commit_audio()
            logger.info("[realtime] Audio committed — streaming output")
            text_parts: list[str] = []
            sentence_buf: str = ""
            first_sentence_sent: bool = False

            for output in realtime.stream_output():
                if isinstance(output, DelegateSignal):
                    delegated = True
                    delegate_msg = output.message
                    continue
                if delegated:
                    continue
                # Native voice: play the model's OWN audio straight to the speaker.
                if native and isinstance(output, RTAudioOutput):
                    if not native_started:
                        native_started = tts.native_play_begin(
                            realtime.output_sample_rate
                        )
                        if native_started:
                            logger.info("[realtime] Native audio → playing model voice")
                    if native_started:
                        tts.native_play_frame(output.audio)
                    if output.transcript:
                        text_parts.append(output.transcript)
                    continue
                if isinstance(output, RTTextOutput):
                    text_parts.append(output.text)
                    if native:
                        # Audio already carries the reply — keep text only for
                        # memory + the [HANDLED] hint; don't synthesize it.
                        continue
                    sentence_buf += output.text
                    # Flush complete sentences to TTS as they arrive
                    if tts is not None and sentence_buf.rstrip().endswith(SENTENCE_ENDS):
                        sentence: str = strip_markers(sentence_buf)
                        if sentence:
                            if not first_sentence_sent:
                                logger.info(
                                    "[realtime] First sentence → speak: %r",
                                    sentence[:80],
                                )
                                tts.speak(sentence)
                                first_sentence_sent = True
                            else:
                                logger.info(
                                    "[realtime] Next sentence → speak_queue: %r",
                                    sentence[:80],
                                )
                                tts.speak_queue(sentence)
                        sentence_buf = ""

            transcript = strip_markers("".join(text_parts))

            # Native playback owns the speaker for the whole turn — release it
            # once all frames are in (records transcript for STT echo cancel).
            # Reset native_started so a later exception's cleanup can't double-end;
            # native_played records that audio actually played (for `spoke` below).
            if native_started:
                tts.native_play_end(transcript)
                native_started = False
                native_played = True

            if delegated:
                logger.info("[realtime] Model delegated → will forward to OS server")
            else:
                # Flush any remaining text that didn't end with a sentence boundary
                # (ElevenLabs path only — native mode never fills sentence_buf).
                remaining: str = strip_markers(sentence_buf)
                if not native and remaining and tts is not None:
                    if not first_sentence_sent:
                        logger.info(
                            "[realtime] Final fragment → speak: %r", remaining[:80]
                        )
                        tts.speak(remaining)
                        first_sentence_sent = True
                    else:
                        logger.info(
                            "[realtime] Final fragment → speak_queue: %r", remaining[:80]
                        )
                        tts.speak_queue(remaining)
                # Only claim the turn as HANDLED if the model actually SPOKE.
                # Native mode → audio actually played (native_played); ElevenLabs
                # mode → a sentence was synthesized OR a transcript exists. An empty
                # result (receive() timed out, or native mode produced no audio) must
                # NOT be reported as handled: that sends [HANDLED] with an empty
                # [REPLY], OpenClaw's input-branching reads it as "already answered"
                # and stays silent. Leaving handled False (delegated also False) falls
                # through to the normal forward below so the main agent answers.
                spoke = native_played if native else (first_sentence_sent or bool(transcript))
                if spoke:
                    handled = True
                    # Label this `agent_reply`, not `transcript`: it is what Moon
                    # SAID, not what the user said. Elsewhere `transcript` means the
                    # user's STT, so reusing the word here reads as role-reversed.
                    logger.info(
                        "[realtime] Chit-chat complete — agent_reply=%r",
                        transcript[:200] if transcript else "(empty)",
                    )
                    # Save turn to realtime memory
                    if combined or transcript:
                        realtime.save_turn(
                            user_text=combined or "(audio only)",
                            agent_text=transcript or "(audio only)",
                        )
                else:
                    # No spoken output from the realtime agent (empty / timeout). Do
                    # NOT claim a forward here — whether the turn actually reaches the
                    # OS server is decided by the caller's `if combined:`. A pure
                    # noise turn with empty STT is correctly dropped.
                    logger.info(
                        "[realtime] No realtime output (empty / timeout) — "
                        "turn falls back to OS server only if STT produced a transcript"
                    )
        except Exception as e:
            logger.warning(
                "[realtime] Processing failed: %s — will forward to OS server", e
            )
            # Release the speaker if native playback was mid-flight (avoids a
            # stuck TTS lock / native_mode flag).
            if native_started:
                try:
                    tts.native_play_end(transcript)
                except Exception:
                    pass
                native_started = False
            delegated = True  # fall through to OS server on error
    elif hal_config.REALTIME_ENABLED and noise_turn:
        logger.info(
            "[realtime] Skipping commit — noise/false-trigger turn "
            "(empty STT, dur=%.2fs, min=%.2fs, silero_speech=%s)",
            buf_duration,
            hal_config.REALTIME_MIN_COMMIT_DURATION_S,
            audio_is_speech,
        )
    elif hal_config.REALTIME_ENABLED:
        logger.warning(
            "[realtime] Enabled but agent not available — falling back to OS server"
        )

    return RealtimeTurnResult(delegated, handled, transcript, delegate_msg)
