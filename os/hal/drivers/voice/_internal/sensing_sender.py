"""POST transcripts to os-server + drop transcripts that echo our own TTS.

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


class SensingSender:
    """Send sensing events to os-server with retry + echo suppression."""

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

    def send(
        self,
        message: str,
        event_type: str = "voice",
        skip_echo: bool = False,
        image_b64: str = "",
    ) -> None:
        """POST decorated message to os-server /api/sensing/event with retry.

        ``image_b64`` (raw base64 JPEG, no data-URI prefix) rides the payload's
        ``image`` field. os-server describes it with a vision model and forwards
        the description as text (describe-first) — required because the main
        model can be text-only: a file path in ``message`` is useless there
        (tool-read image blocks are silently dropped), and a raw attachment
        404s at the smart-agent-router when it picks a no-vision backend.
        """
        if not skip_echo and self.is_echo(message):
            return

        payload = {"type": event_type, "message": message}
        if image_b64:
            payload["image"] = image_b64
        # Log a copy with the image masked — a ~70KB base64 blob would drown the log.
        log_payload = {**payload, "image": f"<{len(image_b64)} b64 chars>"} if image_b64 else payload
        logger.info(
            "curl -s -X POST %s -H 'Content-Type: application/json' -d '%s'",
            OS_SENSING_URL, _json.dumps(log_payload),
        )
        # Image turns wait for the os-server-side vision describe (up to 80s,
        # internal/vision DescribeTimeout — qwen measured 2026-07-06: scene
        # images 8-20s but TEXT-DENSE images 23-38s per attempt) before the
        # response comes back — don't let the plain-text timeout abort them
        # into a spurious "failed to send" warning.
        timeout_s = 90 if image_b64 else 5
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(OS_SENSING_URL, json=payload, timeout=timeout_s)
                if resp.status_code == 503 and attempt < max_retries:
                    logger.warning(
                        "os-server agent not ready (503), retrying in 2s... (attempt %d/%d)",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                    continue
                elif resp.status_code != 200:
                    logger.warning("os-server returned %d: %s", resp.status_code, resp.text)
                else:
                    logger.info("Sent to os-server: %r", message)
                return
            except requests.ConnectionError as e:
                if attempt < max_retries:
                    logger.warning(
                        "os-server not reachable (attempt %d/%d), retrying in 2s...",
                        attempt, max_retries,
                    )
                    time.sleep(2)
                else:
                    logger.warning(
                        "Failed to send voice event to os-server after %d attempts: %s",
                        max_retries, e,
                    )
            except requests.RequestException as e:
                logger.warning("Failed to send voice event to os-server: %s", e)
                return
