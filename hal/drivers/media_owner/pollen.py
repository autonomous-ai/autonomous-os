"""Media handover with Pollen Robotics' reachy_mini daemon (Reachy Mini).

The daemon owns the camera and both ALSA PCMs for its own app runtime, so every
HAL capture fails with "Device or resource busy" until it lets go. It exposes a
supported handover for exactly that: POST /api/media/release gives the devices
to whoever asks, POST /api/media/acquire takes them back. Motion is unaffected —
the daemon keeps running and keeps driving the motors either way.

Why HAL performs the handover itself rather than a launcher doing it before
startup: ordering. The Reachy SDK happens to call release when a client
connects, but that runs inside HAL's motion-init thread and races audio
detection. Losing that race is silent and total — with the daemon still holding
the card, PortAudio cannot probe a single sample rate, the configured ALSA
output never enumerates, and TTS settles on output device -1 and raises on every
utterance while all the status endpoints still report healthy.

Reached over the same host/port the motion driver uses, so a robot that moved
its daemon needs one setting changed, not two.
"""
from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


class PollenDaemonMediaOwner:
    """MediaOwner backed by the reachy_mini daemon's /api/media endpoints."""

    _TIMEOUT_S = 5.0
    # The daemon is a systemd service starting alongside HAL, so its HTTP port
    # may not be listening yet on a cold boot. Retry rather than lose the camera
    # and mic for the whole session over a two-second race.
    _RELEASE_ATTEMPTS = 5
    _RETRY_DELAY_S = 2.0

    def __init__(self, host: str | None = None, port: int | None = None):
        # Same defaults and env names as hal/drivers/motors/reachy_service.py —
        # HAL runs on the robot's own Pi, so the daemon is local.
        self._host = host or os.getenv("REACHY_DAEMON_HOST", "localhost")
        self._port = int(port or os.getenv("REACHY_DAEMON_PORT", "8000"))
        self._base = f"http://{self._host}:{self._port}/api/media"

    def _post(self, action: str) -> bool:
        resp = requests.post(f"{self._base}/{action}", timeout=self._TIMEOUT_S)
        resp.raise_for_status()
        logger.info("[media-owner] pollen %s -> %s", action, resp.text.strip()[:200])
        return True

    def _restore_volume(self) -> None:
        """Put the user's speaker level back after a release.

        The daemon's release handler resets the card's mixer to its own level as
        part of handing over — measured: 90% before the call, 62% after, with
        HAL not running at all and `acquire` provably innocent. Nothing else
        re-asserts it in time: HAL only ever WRITES the persisted level
        (routes/audio.py), and os-server restores it once at its own startup,
        so a plain HAL restart left the speaker at Pollen's level while the
        slider, the persisted file, and the agent all still said the user's.

        Best-effort and quiet when there is no persisted level yet (first boot):
        the daemon's own default is a reasonable thing to be left with.
        """
        try:
            from hal.config import VOLUME_STATE_PATH
            with open(VOLUME_STATE_PATH) as f:
                pct = int(f.read().strip())
        except Exception:
            return
        if not 0 <= pct <= 100:
            return
        try:
            # Through the route rather than a fresh amixer call: it owns the
            # dB envelope and the DAC/BT-sink routing, and duplicating that
            # here would drift from what every other volume path does.
            from hal.models import VolumeRequest
            from hal.routes.audio import set_volume

            set_volume(VolumeRequest(volume=pct))
            logger.info("[media-owner] pollen release reset the mixer — restored %d%%", pct)
        except Exception as e:
            logger.warning(
                "[media-owner] could not restore volume %d%% after release: %s", pct, e
            )

    def release(self) -> bool:
        """Take the camera and audio devices from the daemon."""
        for attempt in range(1, self._RELEASE_ATTEMPTS + 1):
            try:
                ok = self._post("release")
                self._restore_volume()
                return ok
            except Exception as e:
                if attempt == self._RELEASE_ATTEMPTS:
                    logger.warning(
                        "[media-owner] pollen release failed after %d attempts (%s) — "
                        "camera and audio will report 'device busy'",
                        self._RELEASE_ATTEMPTS, e,
                    )
                    return False
                logger.info(
                    "[media-owner] pollen release attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt, self._RELEASE_ATTEMPTS, e, self._RETRY_DELAY_S,
                )
                time.sleep(self._RETRY_DELAY_S)
        return False

    def acquire(self) -> bool:
        """Give the camera and audio devices back to the daemon.

        Single attempt: this is the shutdown path, where systemd is already
        counting down to SIGKILL, and a missed acquire is repaired by the next
        release anyway.
        """
        try:
            return self._post("acquire")
        except Exception as e:
            logger.warning(
                "[media-owner] pollen acquire failed (%s) — daemon stays without media", e
            )
            return False
