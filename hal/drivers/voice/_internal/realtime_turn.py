"""Realtime agent turn handling — extracted from VoiceService._stream_session.

Given the audio already streamed to the realtime model for one speaking turn,
drive the turn (commit → stream output → speak sentences → delegate/handle) and
report the outcome. Pure helper: it touches no VoiceService state beyond the
orchestrator / TTS handles and the marker-stripper passed in.
"""

import logging
from typing import Callable, NamedTuple, Optional

from hal import app_state as hal_app_state
from hal import config as hal_config
from hal import presets
from hal.clock import device_now
from hal.realtime.config import gemini_needs_idle_workaround
from hal.realtime.models import AudioOutput as RTAudioOutput
from hal.realtime.models import TextOutput as RTTextOutput
from hal.realtime.models.signal import DelegateSignal, LookReplaySignal
from hal.drivers.voice._internal.cot_leak_filter import CoTLeakFilter, clean_transcript

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
    from hal.realtime.context_manager.base import ContextManagerBase

    code: str = (_os_cfg_get("stt_language", "") or "").strip()
    if not code:
        return ""
    return ContextManagerBase.LANGUAGE_NAMES.get(code, code)


def _thinking_cue_start() -> None:
    """Show `thinking` while the realtime model works on the committed turn.

    In-session turns (no delegate) had NO visual state between the listening
    cue and the first reply audio — 1-3s of a lamp that looks frozen. Full
    in-process emotion call, same path the agents use.
    """
    try:
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=presets.EMO_THINKING))
        # thinking is a background emotion, so the route's LED path skips it
        # whenever the user has a saved color (guard against per-message hook
        # spam). This cue is deliberate and always cleared — force the purple
        # pulse so the wait is visible. User-LED-off still wins inside.
        hal_app_state._apply_emotion_led_display(
            presets.EMO_THINKING, 0.7, force_led=True
        )
    except Exception as e:
        logger.warning("[realtime] thinking cue failed: %s", e)


def _thinking_cue_clear() -> None:
    """Return to idle when the reply starts (or the turn produced nothing) —
    ONLY if the face still shows our `thinking`, so an emotion the model
    expressed via the express_emotion tool is never stomped."""
    try:
        if hal_app_state._current_emotion != presets.EMO_THINKING:
            return
        from hal.models import EmotionRequest
        from hal.routes.emotion import express_emotion

        express_emotion(EmotionRequest(emotion=presets.EMO_IDLE))
    except Exception as e:
        logger.warning("[realtime] thinking cue clear failed: %s", e)


def build_turn_context(speaker: Optional[str] = None) -> str:
    """Build per-turn context for the realtime model.

    ``speaker`` is the VOICE speaker-ID display name (e.g. "Darren") for this
    turn, resolved just before this call. When present it is authoritative for
    who the model should address and OVERRIDES the face-derived ``current_user``
    (the two identities are separate: voice = who is speaking, face = who is
    seen). When None (no voice ID this turn — unknown / STOI-reject / no
    transcript) we fall back to the face ``current_user`` as before.
    """
    turn_ctx: list[str] = [
        f"Time: {device_now().strftime('%Y-%m-%d %H:%M:%S %A')}",
    ]
    # Per-turn language reminder. The system prompt already locks the language, but
    # Google Search grounding pulls English source text into context and can drag
    # the spoken reply into English. A reminder right next to the turn (closest to
    # generation) is the strongest lever.
    lang_name: str = _reply_language_name()
    if lang_name:
        turn_ctx.append(
            f"Reply language: {lang_name} (answer ONLY in {lang_name}, "
            "even if a search result or any context is in another language)"
        )
    if speaker:
        # Voice speaker-ID wins: reuse the same "Current user:" slot the model
        # already acts on, and phrase it so it overrides any stale identity in
        # session memory (e.g. the previously-seen face).
        turn_ctx.append(
            f"Current user: {speaker} (identified by voice — address this "
            f"person, not anyone else)"
        )
    else:
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
    return "[TURN CONTEXT] " + " | ".join(turn_ctx)


class RealtimeTurnResult(NamedTuple):
    """Outcome of a realtime turn, consumed by the OS-server dispatch step."""

    delegated: bool = False
    handled: bool = False
    transcript: str = ""
    delegate_msg: str = ""


def should_dispatch_to_main(
    wakeword_enabled: bool,
    wake_word_confirmed: bool,
) -> bool:
    """Return whether the finalized STT turn must reach the main agent.

    Wake-word mode suppresses ambient speech until STT has armed the turn. Once
    confirmed, every turn reaches dispatch: a realtime-handled turn becomes a
    `voice_agent_handled` synchronization event, while unavailable, silent,
    failed, and delegated turns take the normal main-agent path. This keeps
    memory and one-turn vision handoff behavior identical to always-listening.
    """
    if not wakeword_enabled:
        return True
    return wake_word_confirmed


def is_noise_turn(
    combined: str, buf_duration: float, audio_is_speech: bool = True
) -> bool:
    """Return whether this capture must not be committed to the realtime model."""
    if hal_config.REALTIME_REQUIRE_TRANSCRIPT:
        return not combined
    return not combined and (
        buf_duration < hal_config.REALTIME_MIN_COMMIT_DURATION_S or not audio_is_speech
    )


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
    #
    # REQUIRE_TRANSCRIPT (default): drop ANY empty-STT turn. The Silero signals
    # below only reject non-speech; real human speech that nova-3 missed (short
    # utterances below its floor) is voiced and would otherwise commit as raw
    # audio, which Gemini fills with an invented greeting. "No transcript" → don't
    # speak. A turn with a transcript always commits.
    #
    # When REQUIRE_TRANSCRIPT is off, fall back to the Silero-gated audio-only
    # path: an empty-STT turn still commits unless (1) it's a sliver of audio (just
    # the VAD pre-roll, below the duration floor), or (2) Silero VAD judged the
    # FULL buffer non-speech (`audio_is_speech` computed by the caller, default
    # True so a missing check never drops a turn).
    noise_turn = is_noise_turn(combined, buf_duration, audio_is_speech)
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
        # Fill the Gemini wait with `thinking` (cleared at first output).
        # Delegated turns keep it — the main agent's wait is even longer.
        _thinking_cue_start()
        try:
            # 1011 recovery (idle-death): the campaign-api proxy drops idle
            # 2.5-native-audio sessions, so a turn that follows a pause lands on a
            # dead session and Gemini returns WS 1011 with NO output. The proxy
            # serves ACTIVE sessions fine (continuous talking works), so on a
            # no-output turn reconnect a FRESH session and REPLAY this turn's
            # already-captured audio IMMEDIATELY (no idle wait) — turning a
            # post-pause turn into an active one. Audio lives in rt_audio_buffer.
            # Gemini only; other providers retry 0 times.
            # Replay only for 2.5 native-audio (the model with the idle-resume WS
            # 1011). 3.1 handles idle→resume fine, so retries=0 there — no churn,
            # no 6-9s dead-air on a no-output turn. See gemini_needs_idle_workaround.
            is_gemini: bool = hal_config.REALTIME_PROVIDER.strip().lower() == "gemini"
            max_retries: int = (
                hal_config.REALTIME_GEMINI_TURN_RETRIES
                if (is_gemini and gemini_needs_idle_workaround())
                else 0
            )
            text_parts: list[str] = []
            sentence_buf: str = ""
            first_sentence_sent: bool = False
            attempt: int = 0
            look_replayed: bool = False  # one look-replay per turn (loop guard)
            # Gemini 3.1 sometimes leaks its chain of thought into the reply text
            # (thinking can't be disabled on that model) — filter it out of both
            # the spoken sentences and the forwarded transcript. See
            # cot_leak_filter.py for the mechanism and evidence.
            reply_lang: str = _reply_language_name()
            leak_filter = CoTLeakFilter(reply_lang)
            while True:
                if attempt > 0:
                    logger.info(
                        "[realtime] No output (likely WS 1011) — fresh session + "
                        "replay audio (retry %d/%d)",
                        attempt,
                        max_retries,
                    )
                    if not realtime.recover_session("gemini-1011-replay"):
                        break
                    for _frame in rt_audio_buffer:
                        realtime.append_audio(_frame)
                    text_parts = []
                    sentence_buf = ""
                    first_sentence_sent = False
                    leak_filter = CoTLeakFilter(reply_lang)

                # Drop any output still queued from a previous turn so this turn
                # only reads its OWN response (stale async replies desync onto a
                # later turn → "talks on its own" + double TTS).
                realtime.flush_output()
                realtime.commit_audio()
                logger.info("[realtime] Audio committed — streaming output")

                look_replay: bool = False
                for output in realtime.stream_output():
                    if isinstance(output, LookReplaySignal):
                        # Model called look and a fresh frame was sent. The Live
                        # API queues mid-turn frames for the NEXT turn, so the
                        # generator stopped this one — re-commit the same audio
                        # below and the frame joins the replayed turn.
                        look_replay = True
                        continue
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
                                _thinking_cue_clear()
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
                            sentence: str = leak_filter.filter_text(
                                strip_markers(sentence_buf)
                            )
                            if sentence:
                                if not first_sentence_sent:
                                    logger.info(
                                        "[realtime] First sentence → speak: %r",
                                        sentence[:80],
                                    )
                                    # speak() returns False when another
                                    # non-interruptible TTS holds the speaker
                                    # (ambient nudge racing the turn) — queue
                                    # the reply instead of losing it entirely.
                                    if not tts.speak(sentence):
                                        tts.speak_queue(sentence)
                                    first_sentence_sent = True
                                    _thinking_cue_clear()
                                else:
                                    logger.info(
                                        "[realtime] Next sentence → speak_queue: %r",
                                        sentence[:80],
                                    )
                                    tts.speak_queue(sentence)
                            sentence_buf = ""

                # Look-replay: re-append this turn's audio to the SAME session
                # (unlike the 1011 recovery below, which needs a fresh one) and
                # loop — flush_output + commit_audio at the top open a new turn
                # that picks up the queued camera frame. The new user activity
                # cancels the pending tool-call turn server-side. Guarded to
                # once per turn; a second signal (shouldn't happen — the replay
                # turn's look hits the reuse path) falls through to the normal
                # exit so it can't loop forever.
                if look_replay and not look_replayed:
                    look_replayed = True
                    logger.info(
                        "[realtime] look: re-committing turn audio so the fresh "
                        "frame joins the replayed turn"
                    )
                    for _frame in rt_audio_buffer:
                        realtime.append_audio(_frame)
                    text_parts = []
                    sentence_buf = ""
                    first_sentence_sent = False
                    leak_filter = CoTLeakFilter(reply_lang)
                    continue

                # A WS-1011 failure yields NOTHING (no audio spoken yet), so a
                # retry is safe. Stop as soon as the turn produced real output.
                produced: bool = (
                    delegated
                    or first_sentence_sent
                    or native_started
                    or bool("".join(text_parts).strip())
                )
                if produced or attempt >= max_retries:
                    break
                attempt += 1

            # Clean the transcript with FRESH filter state (it re-reads the whole
            # turn from the top): this is what gets forwarded as [REPLY], saved to
            # realtime memory, and shown in web chat — a leak here re-enters the
            # model's context next turn and self-reinforces.
            transcript = clean_transcript(strip_markers("".join(text_parts)), reply_lang)

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
                remaining: str = leak_filter.filter_text(strip_markers(sentence_buf))
                if not native and remaining and tts is not None:
                    if not first_sentence_sent:
                        logger.info(
                            "[realtime] Final fragment → speak: %r", remaining[:80]
                        )
                        # Same busy fallback as the first-sentence site above.
                        if not tts.speak(remaining):
                            tts.speak_queue(remaining)
                        first_sentence_sent = True
                        _thinking_cue_clear()
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
                    # Dead turn — don't leave the thinking face hanging, and
                    # return the strip to the user's color (idle is a
                    # background emotion, so the clear alone leaves the forced
                    # purple pulse running). restore_led is TTS-guarded, so a
                    # reply that somehow started speaking keeps its wave.
                    _thinking_cue_clear()
                    try:
                        from hal.routes.led import restore_led

                        restore_led()
                    except Exception:
                        pass
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
            "[realtime] Skipping commit — empty STT, not committing to model "
            "(require_transcript=%s, dur=%.2fs, min=%.2fs, silero_speech=%s)",
            hal_config.REALTIME_REQUIRE_TRANSCRIPT,
            buf_duration,
            hal_config.REALTIME_MIN_COMMIT_DURATION_S,
            audio_is_speech,
        )
        # The dropped turn's audio already streamed into the session's open
        # manual-VAD activity and would be billed with (and can confuse) the
        # NEXT committed turn. Swap in a fresh session — this turn is dead
        # air, so nobody is waiting on the ~1s handshake.
        if rt_audio_buffer and realtime.available:
            try:
                if realtime.discard_open_activity("noise-drop"):
                    logger.info(
                        "[realtime] Discarded open activity after noise drop (fresh session)"
                    )
            except Exception:
                logger.exception("[realtime] noise-drop discard failed")
    elif hal_config.REALTIME_ENABLED:
        logger.warning(
            "[realtime] Enabled but agent not available — falling back to OS server"
        )

    return RealtimeTurnResult(delegated, handled, transcript, delegate_msg)
