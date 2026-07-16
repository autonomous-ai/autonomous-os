"""Dedicated mic-mute slide switch on PD1 (Intern v2 Pro only).

Wiring is a SPST slide switch, not a momentary push button — one position
bridges PD1 to GND, the other leaves it floating (pull-up wins). We track
level, not edges: the switch position IS the mute state, so pressing/
releasing has no meaning here. Every edge flips the mic to whatever the
new level dictates, and we also re-sync at HAL start-up so a boot with
the switch already in the "muted" position ends up with a muted mic
without waiting for the user to toggle it.

Polarity:
- LOW  (0) → switch shorted to GND → mic MUTED
- HIGH (1) → switch open (pull-up)   → mic UNMUTED
Flip `_LEVEL_MUTED` below if the physical wiring is the opposite.

Web admin coexistence: `/api/hardware/voice/mute` (+ /voice/unmute) share
the same `state._mic_muted` variable. If a user mutes via the web while
the switch is in "unmute" position, the discrepancy stands until the
next physical throw — the switch is the authority on the *next* edge,
not continuously. That matches how a hardware kill-switch is expected
to behave.

Device gating: Intern v2 Pro and Lamp share the same board (OrangePi
sun60iw2) but ship different hardware kits — only Intern v2 Pro has a
switch physically wired to PD1. Lamp's PD1 is either unpopulated or used
for something else, so claiming it there would either float and log spam
on stray edges, or actively collide with another driver. We gate on
DEVICE_TYPE (the same env the OS resolves at boot — see
server._resolve_device_type) rather than the board profile so the wiring
declaration follows the physical kit, not the SoC.

Pin choice: PE1 was the obvious candidate from the header silkscreen but
it's already claimed by SPI3_CLK for the WS2812 LED strip (see
boards.json led.spi_bus=3). PD1 sits next to the TTP223 touch lines
(PD0/PD2/PD4 at chip0 lines 96/98/100) and is unclaimed on this board,
so we route the switch there instead.

PD1 wiring:
- PD = pinctrl bank 3 on Allwinner sun60iw2 → chip 0 lines 96–127
- PD1 = chip 0, line 97
- Debounce mirrors the primary wake button (200 ms) — same OrangePi
  contact-bounce characteristics; slide-switch contacts also bounce
  briefly during the throw.
"""

import logging
import os
import threading
import time

import hal.app_state as state

logger = logging.getLogger(__name__)

# PD1 = pinctrl bank 3 (PD) + line 1 → gpiochip0 line 97 on Allwinner
# sun60iw2. Only wired on Intern v2 Pro; see the module docstring.
_MIC_BTN_CHIP = 0
_MIC_BTN_LINE = 97
# Settle time after the LAST edge before we re-read the pin. Slide switch
# contacts bounce for a few ms during the throw; 60 ms covers the bounce
# tail without introducing a noticeable delay for the operator. This is
# the settle window, NOT a "drop-if-within" filter — see _on_edge for the
# timer-restart pattern that makes rapid flips reliable.
_MIC_BTN_SETTLE_SEC = 0.06

# GPIO level that means "muted". Flip if the physical wiring inverts.
_LEVEL_MUTED = 0

# Watchdog reconcile period. lgpio's edge-callback thread has been observed
# to stall silently under sustained edge storms (the HAL process stays up,
# routes still respond, but no more edges fire). Reading the pin every
# _WATCHDOG_SEC and driving state to match the level guarantees the mic
# state converges to the switch's physical position even if we miss every
# edge for a while — cheap safety net, not a replacement for the callback.
_WATCHDOG_SEC = 30.0

# Device types that ship with a mic-mute switch physically wired to PD1.
# Add here when a new device model gets the switch — the driver stays the
# same, only the whitelist grows. Cross-check the wiring before adding: a
# device on the same board but different kit could have PD1 in use for
# something else, in which case claiming it here would misfire.
_DEVICES_WITH_MIC_BUTTON = frozenset({"intern-v2"})


def _resolve_device_type() -> str:
    """Same resolution order as server._resolve_device_type — env first,
    then config.json — so the driver agrees with the rest of HAL about
    which body it's running on. Kept local (rather than imported from
    server) to avoid an import cycle: server imports drivers, not the
    other way around."""
    dev = os.environ.get("DEVICE_TYPE")
    if dev:
        return dev
    try:
        from hal.config import _os_cfg_get

        cfg = _os_cfg_get("device_type")
        if cfg:
            return str(cfg)
    except Exception:
        pass
    return ""


class MicButtonHandler:
    def __init__(self):
        self._lgpio = None
        self._handle = None
        self._callback = None
        # Debounce is done via "restart timer on each edge, read pin when it
        # fires" — see _on_edge. Guards a single settle Timer at a time so
        # rapid flips don't stack N pending reconciles.
        self._settle_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        # Serializes _apply_state against itself so overlapping timers /
        # watchdog ticks can't check-then-write race on state._mic_muted.
        self._apply_lock = threading.Lock()

    def start(self):
        dev = _resolve_device_type()
        if dev not in _DEVICES_WITH_MIC_BUTTON:
            logger.info(
                "Mic switch disabled: device_type=%r (only wired on %s)",
                dev or "<unset>",
                ", ".join(sorted(_DEVICES_WITH_MIC_BUTTON)),
            )
            return

        import lgpio

        self._lgpio = lgpio

        try:
            self._handle = lgpio.gpiochip_open(_MIC_BTN_CHIP)
        except Exception as e:
            logger.warning("Mic switch gpiochip_open(%d) failed: %s", _MIC_BTN_CHIP, e)
            return

        try:
            lgpio.gpio_claim_alert(
                self._handle, _MIC_BTN_LINE, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
            )
            self._callback = lgpio.callback(
                self._handle, _MIC_BTN_LINE, lgpio.BOTH_EDGES, self._on_edge
            )
        except Exception as e:
            logger.warning(
                "Mic switch claim line %d failed: %s -- disabled", _MIC_BTN_LINE, e
            )
            return

        # Sync boot-time state to the switch's current position. Without this,
        # a device that boots with the switch already in "muted" would run
        # with the mic hot until the user throws it once and back.
        try:
            initial_level = lgpio.gpio_read(self._handle, _MIC_BTN_LINE)
        except Exception as e:
            logger.warning("Mic switch initial read failed: %s", e)
            initial_level = 1  # default to unmuted if read fails

        logger.info(
            "Mic mute switch ready on gpiochip%d line %d (PD1, initial level=%d, settle %d ms, watchdog %ds)",
            _MIC_BTN_CHIP,
            _MIC_BTN_LINE,
            initial_level,
            int(_MIC_BTN_SETTLE_SEC * 1000),
            int(_WATCHDOG_SEC),
        )

        # Boot-time sync runs SYNCHRONOUSLY (unlike the edge handler which
        # threads off) so subsequent HAL init phases see the final mic state.
        # Without this, a reboot with the switch already in "muted" would let
        # voice_service start briefly listening before the mute lands, opening
        # a ~hundreds-of-ms window where the mic is hot despite the hardware
        # kill switch being off. The route call is idempotent and cheap
        # (~tens of ms at most) so blocking start() here is worth it.
        with self._apply_lock:
            self._apply_state_locked(initial_level == _LEVEL_MUTED)

        # Watchdog: periodic pin re-read + reconcile. Self-heals if the
        # lgpio edge-callback thread stalls silently. Daemon so it dies with
        # the process; no explicit stop needed.
        threading.Thread(
            target=self._watchdog_loop, daemon=True, name="mic-switch-watchdog"
        ).start()

    def _on_edge(self, chip, gpio, level, tick):
        # Restart the settle timer on every edge. The `level` param from
        # lgpio is deliberately IGNORED here — bouncing contacts can call
        # this several times with alternating levels within a few ms and
        # the last one is not always the terminal position. Instead we
        # re-read the pin in _reconcile after the bounce settles, so the
        # applied state matches the switch's real end position.
        with self._timer_lock:
            if self._settle_timer is not None:
                self._settle_timer.cancel()
            t = threading.Timer(_MIC_BTN_SETTLE_SEC, self._reconcile)
            t.daemon = True
            t.name = "mic-switch-settle"
            self._settle_timer = t
            t.start()

    def _reconcile(self):
        """Read the pin now and drive HAL state to match. Called from the
        settle Timer (after an edge storm quiets) and from the watchdog.
        Runs under _apply_lock so concurrent triggers can't race the
        underlying mute_mic() / unmute_mic() routes."""
        try:
            current_level = self._lgpio.gpio_read(self._handle, _MIC_BTN_LINE)
        except Exception as e:
            logger.warning("Mic switch reconcile read failed: %s", e)
            return
        with self._apply_lock:
            self._apply_state_locked(current_level == _LEVEL_MUTED)

    def _watchdog_loop(self):
        while True:
            time.sleep(_WATCHDOG_SEC)
            try:
                self._reconcile()
                # Re-assert the mic-muted LED overlay if we're still muted.
                # /led/status is a transient effect the way speaking_wave
                # (TTS) or any other overlay is, so an in-progress TTS
                # announcement or a manual /led/effect call will paint over
                # our red cue and then /led/restore back to "no user state"
                # → strip clears. Without this re-assert the operator would
                # see the mic-muted cue vanish after the first TTS speak.
                # apply_state's None-safe: if the switch is None (no PD1
                # switch on this device) we never get here — start() only
                # spawns this loop for whitelisted devices.
                if state._hw_mic_switch_muted is True and state._mic_muted:
                    try:
                        from hal.routes.led import set_led_status
                        from hal.models import LEDStatusRequest

                        set_led_status(LEDStatusRequest(state="mic_muted"))
                    except Exception as e:
                        logger.warning(
                            "Mic switch watchdog LED re-assert failed: %s", e
                        )
            except Exception as e:
                logger.warning("Mic switch watchdog tick failed: %s", e)

    def _apply_state_locked(self, muted: bool):
        """Push mic state to match the switch position. Idempotent — if HAL
        state already matches (web admin just set it, or start-up sync
        found the correct value), skip the route call to avoid log spam
        and needless voice-service restarts.

        MUST be called with _apply_lock held (or from single-threaded
        contexts like boot init) so the check-then-write on state._mic_muted
        isn't racy against another edge/watchdog reconcile.
        """
        # Publish the hardware switch's current position for UI/backend
        # kill-switch enforcement (voice_status + /voice/unmute reject).
        # Write BEFORE the route call so /voice/unmute's own guard sees the
        # updated value if it fires racily on the same tick — otherwise a
        # web unmute could sneak in during the ~tens of ms between our
        # decision and voice_service restart.
        state._hw_mic_switch_muted = muted

        if state._mic_muted == muted:
            return

        if muted:
            # Straight mute: log, red LED overlay fires inside mute_mic, and
            # a short (~120ms) ack chime plays through the still-open speaker
            # so the operator gets an audible confirmation of the gesture.
            # Chime is fired BEFORE mute_mic so it starts while voice_service
            # is still winding down — it hits the speaker at gesture time
            # instead of after the STT teardown latency. Chime alone (no
            # spoken cue) matches the "kill switch" feel; the shutter clunk,
            # not an announcement.
            from hal.routes.voice import mute_mic
            from hal.drivers.button_actions import play_ack_chime

            try:
                logger.info("mic switch → muting")
                play_ack_chime(source="mic-switch")
                mute_mic()
            except Exception as e:
                logger.warning("Mic switch mute failed: %s", e)
            return

        # Coming back ON — silent unmute. No chime, no spoken cue.
        # Rationale learned the hard way: any audio out of the speaker at
        # this instant (chime OR TTS phrase) bleeds into the mic which is
        # opening THIS SAME TICK. VAD trips on the echo, an STT session
        # opens, EMO_LISTENING pulses ("processing" spin on the 8-LED ring),
        # and if the agent replies to the echo transcript we're in a self-
        # talk loop. Feedback ONE-WAY: audible cue on the way OUT (mute), no
        # audible cue on the way IN (unmute). Vision fills the gap: red LED
        # clears in unmute_mic → whatever the voice pipeline paints next.
        logger.info("mic switch → unmuting (silent)")
        try:
            from hal.routes.voice import unmute_mic

            unmute_mic()
        except Exception as e:
            logger.warning("Mic switch unmute failed: %s", e)
