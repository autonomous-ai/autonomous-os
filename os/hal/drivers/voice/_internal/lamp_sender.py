"""POST transcripts to Lamp Server + drop transcripts that echo our own TTS.

`_is_echo` is the third layer of echo handling:
  Layer 1: temporal isolation (mic closed during TTS) — voice_service main loop
  Layer 2: adaptive RMS reverb gate                    — voice_service._wait_for_tts
  Layer 3: transcript similarity filter                — here
"""

import json as _json
import logging
import time
from difflib import SequenceMatcher

import requests

from hal.drivers.voice._internal.config import (
    ECHO_RELEVANCE_WINDOW_S,
    ECHO_SIMILARITY_THRESHOLD,
    OS_SENSING_URL,
)

logger = logging.getLogger("hal.voice")


class LampSender:
    """Send sensing events to Lamp Server with retry + echo suppression."""

    def __init__(self, tts_service=None):
        self._tts = tts_service

    def is_echo(self, transcript: str) -> bool:
        """True if transcript matches our last TTS output above threshold."""
        if not self._tts or not self._tts.last_spoken_text:
            return False
        elapsed = time.time() - self._tts.last_spoken_time
        if elapsed > ECHO_RELEVANCE_WINDOW_S:
            return False
        similarity = SequenceMatcher(
            None, transcript.lower(), self._tts.last_spoken_text.lower(),
        ).ratio()
        if similarity >= ECHO_SIMILARITY_THRESHOLD:
            logger.info(
                "Echo detected (similarity=%.2f): '%s' ≈ TTS:'%s' — dropping",
                similarity, transcript[:60], self._tts.last_spoken_text[:60],
            )
            return True
        return False

    def send(self, message: str, event_type: str = "voice", skip_echo: bool = False) -> None:
        """POST decorated message to Lamp /api/sensing/event with retry."""
        if not skip_echo and self.is_echo(message):
            return

        payload = {"type": event_type, "message": message}
        logger.info(
            "curl -s -X POST %s -H 'Content-Type: application/json' -d '%s'",
            OS_SENSING_URL, _json.dumps(payload),
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(OS_SENSING_URL, json=payload, timeout=5)
                if resp.status_code == 503 and attempt < max_retries:
                    logger.warning(
                        "Lamp agent not ready (503), retrying in 2s... (attempt %d/%d)",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                    continue
                elif resp.status_code != 200:
                    logger.warning("Lamp returned %d: %s", resp.status_code, resp.text)
                else:
                    logger.info("Sent to Lamp: %r", message)
                return
            except requests.ConnectionError as e:
                if attempt < max_retries:
                    logger.warning(
                        "Lamp not reachable (attempt %d/%d), retrying in 2s...",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                else:
                    logger.warning(
                        "Failed to send voice event to Lamp after %d attempts: %s",
                        max_retries, e,
                    )
            except requests.RequestException as e:
                logger.warning("Failed to send voice event to Lamp: %s", e)
                return
