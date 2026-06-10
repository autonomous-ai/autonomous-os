"""OS-shutdown announce for the lifespan shutdown path.

Lives outside server.py so the lifespan stays short. Wired in by
`server.lifespan` right after `yield` — runs once per process exit and
picks one of three audible cues based on what's actually happening:

- Button action already announced (long_press / factory_reset set
  `state._shutdown_announced = True` before kicking the OS command) →
  stay silent, the cached clip is still playing.
- OS-level shutdown/reboot pending (systemd in `stopping` state) →
  speak PHRASE_REBOOT (reboot/kexec target) or PHRASE_SHUTDOWN
  (poweroff/halt target). User hears the board is going down for
  minutes.
- Service-level restart (`systemctl restart hal` from OTA,
  deploy, or dev — OS itself stays `running`) → speak
  PHRASE_SERVICE_RESTART ("Be right back."). User hears the lamp
  blinking but will return in seconds.

Three phrases, deliberately distinct tone, so the user knows from the
cue alone whether to wait or to walk away.
"""

import logging
import subprocess
import time

import hal.app_state as state
from hal.i18n import (
    PHRASE_REBOOT,
    PHRASE_SERVICE_RESTART,
    PHRASE_SHUTDOWN,
    PHRASES_BY_LANG,
)
from hal.presets import DEFAULT_LANG

logger = logging.getLogger(__name__)


def _is_os_stopping() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True, timeout=2.0,
        )
        return out.stdout.strip() == "stopping"
    except Exception:
        return False


def _is_reboot_pending() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        return "reboot.target" in out or "kexec.target" in out
    except Exception:
        return False


def _phrase(key: str) -> str:
    try:
        from hal.config import _lamp_cfg_get
        lang = (_lamp_cfg_get("stt_language") or "").strip()
    except Exception:
        lang = ""
    pool = PHRASES_BY_LANG.get(key, {})
    return pool.get(lang) or pool.get(DEFAULT_LANG, "")


def announce_os_shutdown():
    """Speak the appropriate cue + park servos. Called from
    server.lifespan before any service teardown, so tts_service + servo
    bus are still alive. No-op only when a button action already
    announced; otherwise picks shutdown / reboot / service-restart based
    on systemd state so the user can tell minutes-of-downtime from
    seconds-of-blink by sound alone."""
    if state._shutdown_announced:
        logger.info("shutdown already announced by button action -- skip TTS")
        return

    if _is_os_stopping():
        # OS-level: board going dark. Pick reboot vs shutdown by target.
        is_reboot = _is_reboot_pending()
        kind = "reboot" if is_reboot else "shutdown"
        text = _phrase(PHRASE_REBOOT if is_reboot else PHRASE_SHUTDOWN)
    else:
        # Service-level: OTA, deploy, manual restart. Lamp comes back in
        # seconds; use the lighter cue so the user doesn't think the
        # board is dying.
        kind = "service_restart"
        text = _phrase(PHRASE_SERVICE_RESTART)

    logger.info("lifespan announce: kind=%s text=%r", kind, text)

    if state.tts_service and state.tts_service.available and not state._speaker_muted and text:
        # speak_cached is async — sleep covers playback of the cached clip
        # (matches the 5s used by long_press_action).
        state.tts_service.speak_cached(text)
        time.sleep(5)

    # Park servo before systemd kills the process, otherwise the body
    # slams down mid-pose. Same reasoning as long_press_action Step 2.
    try:
        from hal.routes.servo import release_servos
        release_servos()
    except Exception as e:
        logger.warning("servo release before shutdown failed: %s", e)
