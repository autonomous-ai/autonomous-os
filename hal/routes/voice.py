"""Voice route handlers -- /voice/*, /tts/* endpoints.

Note: ``/voice/strangers*`` (unknown-voice-cluster browsing) lives in
:mod:`hal.routes.speaker` — it's semantic output of the speaker
recognition service, kept next to the rest of that code.
"""

import asyncio
import json
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import hal.app_state as state
from hal.config import AUDIO_INPUT_ALSA, TTS_SPEED, TTS_VOICE, TTS_INSTRUCTIONS
from hal.models import (
    RealtimeHistoryRequest,
    SpeakRequest,
    StatusResponse,
    TTSConfigRequest,
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
    if state.simulation_audio:
        from hal.drivers.voice.virtual_service import VirtualTTSService, VirtualVoiceService
        if not state.tts_service:
            state.tts_service = VirtualTTSService(
                voice=req.tts_voice or TTS_VOICE,
                instructions=req.tts_instructions or TTS_INSTRUCTIONS or None,
            )
        if not state.voice_service:
            state.voice_service = VirtualVoiceService(tts_service=state.tts_service)
        return {"status": "already_running" if state.voice_service.listening else "ok"}
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
        # Boost every name the wake-word gate will listen for. STT decides
        # whether a turn is even heard, and it mis-hears proper nouns it has no
        # reason to expect — "hi lamp" came back as "hi lance", "hello rachel"
        # as "hello risa", and each miss silently drops the whole turn. The
        # agent name alone is not enough: the device type and the permanent
        # "autonomous" alias arm the same gate (see _build_wake_words and
        # voice/_internal/config.py DEFAULT_WAKE_WORDS).
        stt_keywords = state._stt_boost_terms()
        if req.deepgram_api_key and DeepgramSTT:
            stt_provider = DeepgramSTT(api_key=req.deepgram_api_key, keywords=stt_keywords)
        elif AutonomousSTT:
            stt_provider = AutonomousSTT(
                api_key=stt_api_key, base_url=stt_base_url, keywords=stt_keywords
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
        if state._mic_muted:
            # Mute restored from the sidecar (or applied by the physical
            # switch) before the pipeline was built: create the service but
            # don't open the mic. /voice/unmute (or the switch) starts it.
            state.logger.info("Voice pipeline created but not started -- mic muted")
        else:
            state.start_voice_service("voice-pipeline-init")
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


@router.post("/voice/tts/config", response_model=StatusResponse)
def update_tts_config(req: TTSConfigRequest):
    """Apply TTS settings to the running service, no restart.

    The service reads provider, voice and speed per utterance, so setting them
    here takes effect on the next sentence. os-server used to apply a voice
    change with `systemctl restart hal`, which takes the microphone, speaker and
    wake word down with it for ten to fifteen seconds — and any admin click that
    lands in that window is simply lost, because HAL is not listening.

    Only fields that are sent are changed; the rest keep their current values,
    so this is safe to call with a partial config.
    """
    if not state.tts_service:
        raise HTTPException(503, "tts service not running")
    svc = state.tts_service
    backend = svc._backend
    current_key = getattr(backend, "_api_key", "") or ""
    current_base = (getattr(backend, "_base_url", "") or "").rstrip("/")
    # ElevenLabs appends /elevenlabs to base_url; strip it for comparison, the
    # same way the speak-time hot swap does.
    if current_base.endswith("/elevenlabs"):
        current_base = current_base[: -len("/elevenlabs")]
    current_provider = getattr(svc, "_provider", None)

    provider = (req.provider or current_provider or "").strip()
    api_key = (current_key if req.api_key is None else req.api_key).strip()
    base_url = (current_base if req.base_url is None else req.base_url).strip()

    if provider != current_provider or api_key != current_key or base_url != current_base:
        from hal.drivers.voice.tts import create_backend
        if svc.speaking:
            svc.stop()
        try:
            svc._backend = create_backend(
                provider=provider, api_key=api_key, base_url=base_url,
            )
            svc._provider = provider
        except Exception as e:
            state.logger.error("TTS config apply failed: %s", e)
            raise HTTPException(500, f"Failed to apply TTS config: {e}")

    if req.voice:
        svc._voice = req.voice
    if req.speed is not None:
        svc._speed = max(0.25, min(4.0, float(req.speed)))
    state.logger.info(
        "TTS config applied live (provider=%s, voice=%s, speed=%s)",
        svc._provider, svc._voice, svc._speed,
    )
    # The TTS cache is keyed by provider, voice, model and speed, so a change
    # here invalidates every clip the device has ready to play about itself.
    # Re-render them now, on a thread: the operator is waiting on this response,
    # and the point of warming is that nobody ever waits for it.
    threading.Thread(
        target=lambda: svc.warm_lifecycle_phrases(),
        daemon=True,
        name="warm-lifecycle-phrases",
    ).start()
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
    if provider == "piper":
        # Piper voices are model files, so the truth is the filesystem rather
        # than a curated list: whatever .onnx is installed can be selected.
        # `lang` is ignored — a Piper model IS a language, so filtering would
        # only hide models the operator deliberately put there.
        import glob
        import os
        voices_dir = os.environ.get("HAL_PIPER_VOICES", "/opt/piper/voices")
        names = sorted(
            os.path.basename(p)[: -len(".onnx")]
            for p in glob.glob(os.path.join(voices_dir, "*.onnx"))
        )
        return {"provider": provider, "voices": names}
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
    if state.music_service and state.music_service.streaming:
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
            realtime_feedback=req.realtime_feedback,
        )
        if not started:
            raise HTTPException(409, "TTS is busy speaking" if not req.prerender else 503)
        return {"status": "prerendered" if req.prerender else "ok"}
    started = state.tts_service.speak(
        req.text,
        interruptible=req.interruptible,
        realtime_feedback=req.realtime_feedback,
    )
    if not started:
        raise HTTPException(409, "TTS is busy speaking")
    return {"status": "ok"}


@router.post("/voice/realtime/history", response_model=StatusResponse)
def realtime_history(req: RealtimeHistoryRequest):
    """Record a main-agent reply with the realtime agent WITHOUT speaking it.

    The speaking path already feeds history from the on_speak_end hook. This is
    for replies that never get there: os-server drops a cancelled turn's speech
    before it reaches TTS, and without this call the realtime session keeps
    save_main_handoff's "its spoken reply follows" placeholder and never learns
    the answer. Returns skipped (not an error) when realtime is off or no voice
    service is running — os-server fires this best-effort and must not treat a
    realtime-less device as a failure.
    """
    if state.voice_service is None:
        return {"status": "skipped"}
    fed = state.voice_service.feed_realtime_history(req.text, spoken=False)
    return {"status": "ok" if fed else "skipped"}


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
    if state.music_service and state.music_service.streaming:
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
    ok = state.tts_service.speak_queue(
        req.text,
        interruptible=req.interruptible,
        realtime_feedback=req.realtime_feedback,
        turn_id=req.turn_id,
        turn_seq=req.turn_seq,
    )
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
    # LED + sidecar BEFORE the pipeline teardown: voice_service.stop() can
    # block for seconds (session teardown), and the mute feedback must not
    # wait that out.
    state._apply_mic_muted_led()
    state._persist_mic_state()
    if state.voice_service and state.voice_service.available:
        # voice_service.stop() has been observed to block for 20-30s when
        # realtime.stop's LLM memory-summarization call hangs on an
        # unresponsive backend (Cloudflare 524, network stall). Detaching
        # to a daemon thread lets this route return in ms — critical because
        # the caller is often the mic-switch driver, which holds an
        # apply_lock while inside this route. If that lock stays held for
        # 27s, the driver can't process the user's next flip (verified in
        # trace: unmute reconcile blocked 11s waiting for the mute lock).
        # Voice service already tolerates parallel start/stop via its
        # _running flag; a small race with a subsequent unmute is
        # acceptable — realtime reconnect logic self-heals.
        threading.Thread(
            target=state.voice_service.stop,
            daemon=True,
            name="voice-mute-teardown",
        ).start()
    state.logger.info("Mic muted by user (voice_service.stop() dispatched to bg thread)")
    return {"status": "ok"}


@router.post("/voice/unmute", response_model=StatusResponse)
def unmute_mic():
    """Unmute mic -- restart voice pipeline."""
    # HW kill-switch beats software: while the physical PD1 slide switch is
    # muted, the web/API is not allowed to override it — mic_button.py would
    # just flip it back on the next reconcile anyway, leaving the UI briefly
    # showing "unmuted" while the pipeline stays down. 409 lets the web toast
    # a specific "flip the switch first" message.
    if state._hw_mic_switch_muted is True:
        raise HTTPException(409, "Hardware mic switch is off -- flip the physical switch to unmute")
    if not state._mic_muted:
        return {"status": "already_unmuted"}
    state._mic_muted = False
    state._mic_manual_override = False
    state.start_voice_service("mic-unmute")
    state._clear_mic_muted_led()
    state._persist_mic_state()
    state.logger.info("Mic unmuted")
    return {"status": "ok"}


def _sound_perception():
    """Sensing-mic SoundPerception instance, or None when sensing is down.

    Same private-attribute access path the face endpoints use
    (state.sensing_service._perception_orchestrator._processors.*).
    """
    if not state.sensing_service:
        return None
    try:
        return state.sensing_service._perception_orchestrator._processors.sound_recognizer
    except AttributeError:
        return None


@router.get("/voice/mic-level")
async def mic_level_stream(request: Request):
    """Stream live mic input levels as Server-Sent Events (~10Hz).

    Each event: `data: {"level", "threshold", "active", "muted",
    "sensing_level", "sensing_age_s", "sensing_threshold",
    "tts_speaking", "music_playing"}`.

    - `level` — voice-pipeline mic (STT), latest capture-frame RMS on int16
      scale (0..32768, computed anyway by the VAD loop — zero added DSP
      cost). Falls to 0 while the mic drains under TTS/music or the
      pipeline is down. `threshold` is the VAD wake threshold.
    - `sensing_level` / `sensing_age_s` — noise mic (SoundPerception on
      HAL_AUDIO_SENSING_DEVICE): the last 0.5s sample's RMS and how old it
      is. Sampled once per sensing poll (a few seconds apart, paused
      during/after TTS), NOT continuous — the web bar steps rather than
      pumps. null when sensing/sound perception isn't running.
      `sensing_threshold` is the loud-noise threshold.
    - `tts_speaking` / `music_playing` — live playback state, piggybacked so
      the web audio card flips "Speaking…/Playing music" the moment playback
      ends instead of waiting out its 5s `/voice/status` poll.

    Consumed by the web Overview audio card (VU meters) via the os-server
    `/api/hardware` proxy — httputil.ReverseProxy streams event-stream
    responses unbuffered, same as the MJPEG camera stream.
    """
    try:
        from hal.drivers.voice._internal.config import RMS_THRESHOLD as vad_threshold
    except ImportError:
        vad_threshold = 0
    from hal.config import SOUND_RMS_THRESHOLD as sound_threshold

    async def gen():
        while not await request.is_disconnected():
            vs = state.voice_service
            active = bool(vs and vs.available and getattr(vs, "_running", False))
            level = float(getattr(vs, "mic_level", 0.0)) if vs else 0.0

            sensing_level = None
            sensing_age_s = None
            sp = _sound_perception()
            if sp is not None:
                s_rms, s_ts = sp.last_level
                if s_ts > 0:
                    sensing_level = round(float(s_rms), 1)
                    sensing_age_s = round(time.time() - s_ts, 1)

            payload = json.dumps(
                {
                    "level": round(level, 1),
                    "threshold": vad_threshold,
                    "active": active,
                    "muted": state._mic_muted,
                    # present = sound perception exists (noise bar should render,
                    # even before its first sample); level stays null until then.
                    "sensing_present": sp is not None,
                    "sensing_level": sensing_level,
                    "sensing_age_s": sensing_age_s,
                    "sensing_threshold": sound_threshold,
                    "tts_speaking": state._tts_speaking,
                    # Same source as GET /audio/status "playing" (music flag,
                    # not the stricter MusicService.streaming).
                    "music_playing": bool(state.music_service.playing)
                    if state.music_service
                    else False,
                }
            )
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        "hw_mic_switch_muted": state._hw_mic_switch_muted,
    }


# Piper install + voice download live in their own module but mount under this
# router: they are not a hardware capability, so they must not need a ROBOT.md
# declaration of their own, and `voice` is already mounted wherever audio is.
# Guarded: an OTA lands files one at a time, so this module can briefly exist
# on a device where hal/routes/piper.py does not. An unguarded import would
# take the WHOLE voice router down with it — no TTS, no STT, a mute device —
# to add a feature nobody had asked for yet. Losing the install endpoints is
# the correct failure here; losing speech is not.
try:
    from hal.routes.piper import router as _piper_router  # noqa: E402
    router.include_router(_piper_router)
except Exception as _e:  # pragma: no cover - depends on partial deployments
    state.logger.warning("Piper install routes unavailable: %s", _e)
