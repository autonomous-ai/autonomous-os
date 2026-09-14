"""Read an authoritative, per-capture Harness voice routing snapshot."""

import logging

import requests

logger = logging.getLogger("hal.voice")
VOICE_MODE_URL = "http://127.0.0.1:5000/api/harness/voice-mode"


def read_voice_mode() -> dict:
    """Fail closed when OS cannot establish which agent owns the microphone."""
    try:
        response = requests.get(VOICE_MODE_URL, timeout=0.5)
        response.raise_for_status()
        envelope = response.json()
        data = envelope.get("data")
        if (
            envelope.get("status") != 1
            or not isinstance(data, dict)
            or type(data.get("enabled")) is not bool
            or type(data.get("generation")) is not int
            or data["generation"] < 0
        ):
            raise ValueError("invalid Harness voice mode response")
        return {"enabled": data["enabled"], "generation": data["generation"]}
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Harness voice mode unavailable; refusing agent dispatch: %s", exc)
        return {"enabled": False, "generation": -1, "unavailable": True}


def bypass_realtime(snapshot: dict) -> bool:
    return snapshot.get("unavailable", False) or snapshot["enabled"]
