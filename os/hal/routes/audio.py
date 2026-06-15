"""Audio route handlers -- /audio devices, /audio/volume, /audio/play-tone, /audio/record."""

import io
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import hal.app_state as state
from hal.config import AUDIO_OUTPUT_ALSA
from hal.models import (
    AudioDevicesResponse,
    StatusResponse,
    VolumeRequest,
    VolumeResponse,
)

router = APIRouter(tags=["Audio"])

# Lazy imports
sd = None
np = None
try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    pass


def _amixer_ctl_device() -> Optional[str]:
    """Derive the amixer control (-D) device from HAL_AUDIO_OUTPUT_ALSA.

    The env names a PCM (e.g. 'plug:device_speaker'), but amixer needs a control
    device. Our asound.conf defines `ctl.<alias>` alongside every `pcm.<alias>`,
    so the bare alias ('device_speaker') is a valid amixer -D target pointing at the
    real speaker card. Returning None falls back to amixer's default card (card 0
    — often the camera mic), so volume changes silently miss the speaker.
    """
    val = AUDIO_OUTPUT_ALSA
    if not val:
        return None
    # Strip a leading ALSA plugin prefix (plug:/plughw:/hw:/dmix:) -> bare spec.
    rest = val.split(":", 1)[1] if ":" in val else val
    rest = rest.strip()
    if not rest:
        return None
    if "," in rest:  # hw-style "card,device" -> control is the card
        rest = rest.split(",", 1)[0]
    if rest.isdigit():  # bare card index -> hw:N
        return f"hw:{rest}"
    return rest  # named alias (ctl.<alias>) or card id


def _detect_playback_controls() -> tuple[list[str], Optional[str]]:
    """Return (playback_control_names, amixer_ctl_device) from amixer."""
    dev = _amixer_ctl_device()
    cmd = ["amixer", "-D", dev, "scontrols"] if dev else ["amixer", "scontrols"]
    _CAPTURE_KEYWORDS = {"capture", "mic", "gain control", "agc", "auto gain"}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            all_controls = re.findall(r"Simple mixer control '([^']+)'", result.stdout)
            playback = [c for c in all_controls if not any(k in c.lower() for k in _CAPTURE_KEYWORDS)]
            return playback, dev
    except Exception:
        pass
    return [], dev


@router.get("/audio", response_model=AudioDevicesResponse)
def get_audio_info():
    """Get audio device availability."""
    return {
        "output_device": state.audio_output_device,
        "input_device": state.audio_input_device,
        "available": state.audio_output_device is not None or state.audio_input_device is not None,
    }


# DAC-only controls (e.g. WM8960 / Rockchip on OrangePi) take dB, not percent.
# Map 0-100% linearly onto this dB envelope; +2dB ceiling avoids speaker overdrive.
_DAC_MAX_DB = 2.0
_DAC_MIN_DB = -60.0
_DAC_CONTROLS = {"DACL", "DACR", "DAC"}


def _pct_to_db(pct: int) -> float:
    pct = max(0, min(100, pct))
    return _DAC_MIN_DB + (pct / 100.0) * (_DAC_MAX_DB - _DAC_MIN_DB)


def _db_to_pct(db: float) -> int:
    span = _DAC_MAX_DB - _DAC_MIN_DB
    pct = round((db - _DAC_MIN_DB) / span * 100.0)
    return max(0, min(100, pct))


@router.post("/audio/volume", response_model=StatusResponse)
def set_volume(req: VolumeRequest):
    """Set system speaker volume (0-100%)."""
    controls, dev = _detect_playback_controls()
    if not controls:
        raise HTTPException(503, "No audio mixer controls found")
    cmd_prefix = ["amixer", "-D", dev] if dev else ["amixer"]
    pct = max(0, min(100, req.volume))
    dac_db = _pct_to_db(pct)
    for ctrl in controls:
        value = f"{dac_db:.1f}dB" if ctrl.upper() in _DAC_CONTROLS else f"{pct}%"
        try:
            # `--` so amixer doesn't parse a leading `-` in negative dB as a flag.
            subprocess.run(
                [*cmd_prefix, "sset", ctrl, "--", value],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            pass
    return {"status": "ok"}


@router.get("/audio/volume", response_model=VolumeResponse)
def get_volume():
    """Get current speaker volume from amixer.

    Reads back through the same envelope `set_volume` writes through:
      - DAC controls -> parse [X.XdB] and map back via _db_to_pct
      - everything else -> parse [NN%] directly
    DAC controls are tried first so round-trip is stable on codecs whose raw%
    range differs from our [-60dB, +2dB] envelope (e.g. WM8960, Rockchip).
    """
    controls, dev = _detect_playback_controls()
    cmd_prefix = ["amixer", "-D", dev] if dev else ["amixer"]
    sorted_controls = sorted(
        controls, key=lambda c: 0 if c.upper() in _DAC_CONTROLS else 1
    )
    for ctrl in sorted_controls:
        try:
            result = subprocess.run(
                [*cmd_prefix, "sget", ctrl],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue
            if ctrl.upper() in _DAC_CONTROLS:
                db_match = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", result.stdout)
                if db_match:
                    return {"control": ctrl, "volume": _db_to_pct(float(db_match.group(1)))}
            pct_match = re.search(r"\[(\d+)%\]", result.stdout)
            if pct_match:
                return {"control": ctrl, "volume": int(pct_match.group(1))}
        except Exception:
            continue
    raise HTTPException(503, "Audio volume control not available")


@router.post("/audio/play-tone", response_model=StatusResponse)
def play_tone(frequency: int = 440, duration_ms: int = 500):
    """Play a test tone through the speaker."""
    if not sd or not np:
        raise HTTPException(503, "Audio not available")
    if state.audio_output_device is None:
        raise HTTPException(503, "No output audio device found")
    # Release the TTS persistent stream so sd.play can grab the ALSA device
    # exclusively. TTS reopens lazily on the next speak() call.
    if state.tts_service and hasattr(state.tts_service, "release_stream"):
        state.tts_service.release_stream()
    dev_info = sd.query_devices(state.audio_output_device)
    sample_rate = int(dev_info["default_samplerate"])
    t = np.linspace(
        0, duration_ms / 1000, int(sample_rate * duration_ms / 1000), endpoint=False
    )
    tone = 0.5 * np.sin(2 * np.pi * frequency * t).astype(np.float32)
    sd.play(tone, samplerate=sample_rate, device=state.audio_output_device)
    return {"status": "ok"}


@router.post("/audio/record")
def record_audio(duration_ms: int = 3000):
    """Record audio from the microphone. Returns WAV bytes."""
    if not sd or not np:
        raise HTTPException(503, "Audio not available")
    if state.audio_input_device is None:
        raise HTTPException(503, "No input audio device found")
    import wave

    dev_info = sd.query_devices(state.audio_input_device)
    sample_rate = int(dev_info["default_samplerate"])
    channels = 1
    frames = int(sample_rate * duration_ms / 1000)
    recording = sd.rec(
        frames,
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
        device=state.audio_input_device,
    )
    sd.wait()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(recording.tobytes())
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")
