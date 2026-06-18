"""Voice route handlers -- /voice/*, /tts/* endpoints.

Note: ``/voice/strangers*`` (unknown-voice-cluster browsing) lives in
:mod:`hal.routes.speaker` — it's semantic output of the speaker
recognition service, kept next to the rest of that code.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal.config import AUDIO_INPUT_ALSA, TTS_SPEED, TTS_VOICE, TTS_INSTRUCTIONS
from hal.models import (
    SpeakRequest,
    StatusResponse,
    VoiceConfigRequest,
    VoiceStartRequest,
    VoiceStatusResponse,
)

router = APIRouter(tags=["Voice"])

# Lazy imports
sd = None
np = None
VoiceService = None
DeepgramSTT = None
AutonomousSTT = None
TTSService = None

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    pass

try:
    from hal.drivers.voice.stt import AutonomousSTT
    from hal.drivers.voice.stt import DeepgramSTT
    from hal.drivers.voice.voice_service import VoiceService
except ImportError:
    pass

try:
    from hal.drivers.voice.tts import TTSService
    from hal.drivers.voice.tts import PROVIDER_OPENAI
except ImportError:
    PROVIDER_OPENAI = "openai"


@router.post("/voice/start", response_model=StatusResponse)
def start_voice(req: VoiceStartRequest):
    """Start the voice pipeline (always-on Deepgram STT + TTS)."""
    voice = req.tts_voice or TTS_VOICE
    instructions = req.tts_instructions or TTS_INSTRUCTIONS or None
    # Resolve per-role credentials with fallback to the LLM defaults so
    # households with one shared credential keep working.
    tts_api_key = req.tts_api_key or req.llm_api_key
    tts_base_url = req.tts_base_url or req.llm_base_url
    stt_api_key = req.stt_api_key or req.llm_api_key
    stt_base_url = req.stt_base_url or req.llm_base_url

    need_tts = TTSService and (
        not (state.tts_service and state.tts_service.available)
        or (state.tts_service and state.tts_service._voice != voice)
        or (state.tts_service and getattr(state.tts_service, "_instructions", None) != instructions)
        or (state.tts_service and getattr(state.tts_service, "_provider", None) != req.tts_provider)
    )
    if need_tts:
        if state.tts_service and state.tts_service.speaking:
            state.tts_service.stop()
        # Release the old service's persistent OutputStream BEFORE creating
        # the new one. Otherwise the new TTSService.__init__ rate probe
        # fails on every rate (device busy) and never writes audio_rate.json,
        # leaving us probe-less until next restart.
        if state.tts_service and hasattr(state.tts_service, "release_stream"):
            try:
                state.tts_service.release_stream()
            except Exception:
                pass
        try:
            state.tts_service = TTSService(
                api_key=tts_api_key,
                base_url=tts_base_url,
                sound_device_module=sd,
                numpy_module=np,
                output_device=state.audio_output_device,
                voice=voice,
                speed=TTS_SPEED,
                instructions=instructions,
                on_speak_start=state._on_tts_speak_start,
                on_speak_end=state._on_tts_speak_end,
                provider=req.tts_provider,
            )
            state.logger.info("TTSService started (provider=%s, voice=%s)", req.tts_provider, voice)
            if state.music_service:
                state.music_service._tts_service = state.tts_service
        except Exception as e:
            state.logger.warning(f"TTSService failed: {e}")

    if state.voice_service and state.voice_service.available:
        if need_tts and state.tts_service:
            state.voice_service._tts = state.tts_service
            if hasattr(state.voice_service, '_backchannel') and state.voice_service._backchannel:
                state.voice_service._backchannel._tts = state.tts_service
            state.logger.info("Updated TTS in running voice service (voice=%s)", voice)
        return {"status": "already_running"}
    if not VoiceService:
        raise HTTPException(503, "Voice service not available (missing deps)")
    try:
        stt_provider = None
        if req.deepgram_api_key and DeepgramSTT:
            agent_name = state._read_agent_name()
            stt_provider = DeepgramSTT(api_key=req.deepgram_api_key, keywords=[f"{agent_name}:3"])
        elif AutonomousSTT:
            stt_provider = AutonomousSTT(
                api_key=stt_api_key, base_url=stt_base_url
            )
        if not stt_provider:
            raise HTTPException(503, "No STT provider available")
        wake_words = state._build_wake_words(state._read_agent_name())
        state.voice_service = VoiceService(
            stt_provider=stt_provider,
            input_device=state.audio_input_device,
            tts_service=state.tts_service,
            music_service=state.music_service,
            wake_words=wake_words,
            alsa_device=AUDIO_INPUT_ALSA,
        )
        state.voice_service.start()
        return {"status": "ok"}
    except Exception as e:
        state.voice_service = None
        raise HTTPException(500, f"Failed to start voice: {e}")


@router.post("/voice/stop", response_model=StatusResponse)
def stop_voice():
    """Stop the voice pipeline."""
    if state.voice_service:
        state.voice_service.stop()
        state.voice_service = None
    if state.tts_service and hasattr(state.tts_service, "release_stream"):
        try:
            state.tts_service.release_stream()
        except Exception:
            pass
    state.tts_service = None
    return {"status": "ok"}


@router.post("/voice/config", response_model=StatusResponse)
def update_voice_config(req: VoiceConfigRequest):
    """Update voice pipeline config at runtime."""
    if not state.voice_service:
        return {"status": "ok"}
    state.voice_service.set_wake_words(req.wake_words)
    return {"status": "ok"}


@router.get("/voice/voices")
def get_voices(provider: Optional[str] = None, lang: Optional[str] = None):
    """Return available TTS voices for the requested (or current) provider.

    `lang` is a BCP-47 stt_language code (e.g. "vi", "zh-CN"). When set,
    ElevenLabs voices are filtered to that language's curated bucket so
    VN/CN owners see only voices that sound natural in their language.
    Empty / unknown lang returns the full flat list (back-compat for
    older clients that don't send lang). OpenAI voices ignore lang —
    its built-in voices are language-agnostic.
    """
    from hal.drivers.voice.tts import ElevenLabsTTSBackend
    from hal.drivers.voice.tts import PROVIDER_ELEVENLABS, PROVIDER_OPENAI as _PO
    if provider is None:
        provider = getattr(state.tts_service, "_provider", _PO) if state.tts_service else _PO
    if provider == PROVIDER_ELEVENLABS:
        return {
            "provider": provider,
            "voices": ElevenLabsTTSBackend.voices_for_language(lang or ""),
        }
    return {"provider": provider, "voices": ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]}


@router.post("/voice/speak", response_model=StatusResponse)
def speak_text(req: SpeakRequest):
    """Synthesize text to speech and play through the speaker."""
    if not state.tts_service:
        state.logger.error("POST /voice/speak: tts_service is None (not initialized)")
        raise HTTPException(
            503,
            "TTS not initialized -- call /voice/start first or check config has llm_api_key + llm_base_url",
        )
    if state._speaker_muted:
        state.logger.info("POST /voice/speak: suppressed -- speaker muted (text='%s')", req.text[:80])
        return {"status": "suppressed"}
    if state.music_service and state.music_service.playing:
        state.logger.info(
            "POST /voice/speak: rejected -- music is playing (text='%s')", req.text[:80]
        )
        raise HTTPException(409, "Speaker busy -- music is playing")

    # Optional provider hot-swap for web TTS preview (test before saving config).
    # Only swap when something actually changed -- comparing values instead of
    # truthiness, so passing the same api_key/base_url every request is a no-op.
    if req.provider:
        current_backend = state.tts_service._backend
        current_provider = getattr(state.tts_service, "_provider", None)
        current_api_key = getattr(current_backend, "_api_key", "") or ""
        current_base_url = getattr(current_backend, "_base_url", "") or ""
        # ElevenLabs appends /elevenlabs to base_url; strip it for comparison.
        normalized_current_base = current_base_url.rstrip("/")
        if normalized_current_base.endswith("/elevenlabs"):
            normalized_current_base = normalized_current_base[: -len("/elevenlabs")]
        wanted_api_key = (req.tts_api_key or current_api_key).strip()
        wanted_base_url = (req.tts_base_url or normalized_current_base).strip()
        needs_swap = (
            req.provider != current_provider
            or wanted_api_key != current_api_key
            or wanted_base_url != normalized_current_base
        )
        if needs_swap:
            from hal.drivers.voice.tts import create_backend
            if state.tts_service.speaking:
                state.tts_service.stop()
            try:
                state.tts_service._backend = create_backend(
                    provider=req.provider, api_key=wanted_api_key, base_url=wanted_base_url,
                )
                state.tts_service._provider = req.provider
                state.logger.info(
                    "TTS backend hot-swapped (provider=%s, base_url=%s)",
                    req.provider, wanted_base_url,
                )
            except Exception as e:
                state.logger.error("TTS backend swap failed: %s", e)
                raise HTTPException(500, f"Failed to swap TTS backend: {e}")

    if not state.tts_service.available:
        state.logger.error(
            "POST /voice/speak: tts_service not available -- backend=%s, sd=%s",
            state.tts_service._backend is not None and state.tts_service._backend.available,
            state.tts_service._sd is not None,
        )
        raise HTTPException(
            503, "TTS not available -- missing openai SDK or sounddevice"
        )
    if req.voice:
        state.tts_service._voice = req.voice
    # Don't dump req.model_dump_json() — it contains tts_api_key. Log shape only.
    state.logger.info(
        "POST /voice/speak: provider=%s voice=%s len=%d interruptible=%s cached=%s prerender=%s",
        req.provider or "(default)",
        req.voice or "(default)",
        len(req.text or ""),
        req.interruptible,
        req.cached,
        req.prerender,
    )
    if req.cached or req.prerender:
        started = state.tts_service.speak_cached(
            req.text,
            interruptible=req.interruptible,
            prerender=req.prerender,
        )
        if not started:
            raise HTTPException(409, "TTS is busy speaking" if not req.prerender else 503)
        return {"status": "prerendered" if req.prerender else "ok"}
    started = state.tts_service.speak(req.text, interruptible=req.interruptible)
    if not started:
        raise HTTPException(409, "TTS is busy speaking")
    return {"status": "ok"}


@router.post("/voice/speak-queue", response_model=StatusResponse)
def speak_queue_text(req: SpeakRequest):
    """Speak text, queueing if TTS is currently busy.

    Differs from /voice/speak: when the speaker is already in use, /voice/speak
    returns 409 and the caller drops the text; /voice/speak-queue accepts the
    request, pre-synthesizes the audio in the background, and plays it
    seamlessly when the current speech finishes (same open ALSA stream → no
    TTFB gap between sentences). Used by the SSE handler so a multi-sentence
    agent reply that streams sentence-by-sentence is heard as one continuous
    utterance instead of N choppy speak() calls separated by ~400ms each.

    409 is still returned when music is playing (speaker fully committed) and
    503 when TTS isn't initialized; both match /voice/speak's contract so
    upstream error handling stays uniform.
    """
    if not state.tts_service:
        state.logger.error("POST /voice/speak-queue: tts_service is None (not initialized)")
        raise HTTPException(503, "TTS not initialized")
    if state._speaker_muted:
        state.logger.info("POST /voice/speak-queue: suppressed -- speaker muted")
        return {"status": "suppressed"}
    if state.music_service and state.music_service.playing:
        state.logger.info("POST /voice/speak-queue: rejected -- music is playing")
        raise HTTPException(409, "Speaker busy -- music is playing")
    if not state.tts_service.available:
        raise HTTPException(503, "TTS not available")
    if req.voice:
        state.tts_service._voice = req.voice
    state.logger.info(
        "POST /voice/speak-queue: len=%d interruptible=%s",
        len(req.text or ""),
        req.interruptible,
    )
    ok = state.tts_service.speak_queue(req.text, interruptible=req.interruptible)
    if not ok:
        raise HTTPException(503, "TTS not available")
    return {"status": "ok"}


@router.post("/tts/stop", response_model=StatusResponse)
def stop_tts():
    """Interrupt active TTS playback immediately."""
    if state.tts_service:
        state.tts_service.stop()
    return {"status": "ok"}


@router.post("/voice/mute", response_model=StatusResponse)
def mute_mic():
    """Mute mic -- stop voice pipeline and sound perception."""
    if state._mic_muted:
        return {"status": "already_muted"}
    state._mic_muted = True
    state._mic_manual_override = True
    if state.voice_service and state.voice_service.available:
        state.voice_service.stop()
    state.logger.info("Mic muted by user")
    return {"status": "ok"}


@router.post("/voice/unmute", response_model=StatusResponse)
def unmute_mic():
    """Unmute mic -- restart voice pipeline."""
    if not state._mic_muted:
        return {"status": "already_unmuted"}
    state._mic_muted = False
    state._mic_manual_override = False
    if state.voice_service:
        state.voice_service.start()
    state.logger.info("Mic unmuted")
    return {"status": "ok"}


@router.get("/voice/status", response_model=VoiceStatusResponse)
def voice_status():
    """Get voice pipeline status."""
    tts_detail = None
    if state.tts_service:
        tts_detail = {
            "has_backend": state.tts_service._backend is not None and state.tts_service._backend.available,
            "has_sd": state.tts_service._sd is not None,
            "provider": getattr(state.tts_service, "_provider", "unknown"),
        }
    return {
        "voice_available": state.voice_service is not None and state.voice_service.available
        if state.voice_service
        else False,
        "voice_listening": state.voice_service.listening if state.voice_service else False,
        "tts_available": state.tts_service is not None and state.tts_service.available
        if state.tts_service
        else False,
        "tts_speaking": state.tts_service.speaking if state.tts_service else False,
        "tts_detail": tts_detail,
        "mic_muted": state._mic_muted,
    }
