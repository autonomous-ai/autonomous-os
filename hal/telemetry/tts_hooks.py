"""Playback hooks handed to every TTSService instance.

Lives here, not at a construction site, because TTSService is built in more
than one place: at boot (hal/server.py) and again on every /voice/start, which
hot-swaps the service when the provider or voice changes. A hook wired at only
one of those silently stops measuring after the first swap — audio keeps
playing, the metrics just go blind.

Measurement only; never raises into the audio path.
"""

import logging

logger = logging.getLogger("hal.telemetry")


def on_playback_audio(owner: str, kind_hint: str) -> None:
    """First real audio frame of a playback reached the stream."""
    try:
        from hal import app_state as state
        from hal.telemetry import voice_metrics

        voice_metrics.playback_audio(owner, kind_hint, state.tts_service)
    except Exception:
        logger.exception("[voice-metrics] playback audio hook failed")


def on_playback_done() -> None:
    """Playback finished or was interrupted."""
    try:
        from hal.telemetry import voice_metrics

        voice_metrics.playback_end()
    except Exception:
        logger.exception("[voice-metrics] playback done hook failed")
