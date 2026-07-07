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

import hal.app_state as state

logger = logging.getLogger(__name__)

# PD1 = pinctrl bank 3 (PD) + line 1 → gpiochip0 line 97 on Allwinner
# sun60iw2. Only wired on Intern v2 Pro; see the module docstring.
_MIC_BTN_CHIP = 0
_MIC_BTN_LINE = 97
_MIC_BTN_DEBOUNCE_NS = 200_000_000

# GPIO level that means "muted". Flip if the physical wiring inverts.
_LEVEL_MUTED = 0

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
        # Per-edge debounce ticks. Slide switches bounce for a few ms during
        # the throw; same 200 ms window the primary wake button uses.
        self._last_edge_tick = 0

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
            "Mic mute switch ready on gpiochip%d line %d (PD1, initial level=%d, debounce %d ms)",
            _MIC_BTN_CHIP,
            _MIC_BTN_LINE,
            initial_level,
            _MIC_BTN_DEBOUNCE_NS // 1_000_000,
        )

        # Boot-time sync runs SYNCHRONOUSLY (unlike the edge handler which
        # threads off) so subsequent HAL init phases see the final mic state.
        # Without this, a reboot with the switch already in "muted" would let
        # voice_service start briefly listening before the mute lands, opening
        # a ~hundreds-of-ms window where the mic is hot despite the hardware
        # kill switch being off. The route call is idempotent and cheap
        # (~tens of ms at most) so blocking start() here is worth it.
        self._apply_state(initial_level == _LEVEL_MUTED)

    def _on_edge(self, chip, gpio, level, tick):
        # Debounce: any edge within the window of the previous edge is a
        # contact-bounce artefact. One combined tick works here (unlike the
        # push button's press/release split) because the switch's two states
        # are symmetric — either can be the "leading" edge.
        if tick - self._last_edge_tick < _MIC_BTN_DEBOUNCE_NS:
            return
        self._last_edge_tick = tick

        # Snap mic state to the new switch position. Off-thread so the lgpio
        # callback returns promptly; route handlers can take tens of ms.
        threading.Thread(
            target=self._apply_state,
            args=(level == _LEVEL_MUTED,),
            daemon=True,
            name="mic-switch-apply",
        ).start()

    def _apply_state(self, muted: bool):
        """Push mic state to match the switch position. Idempotent — if HAL
        state already matches (web admin just set it, or start-up sync
        found the correct value), skip the route call to avoid log spam
        and needless voice-service restarts."""
        if state._mic_muted == muted:
            return

        from hal.routes.voice import mute_mic, unmute_mic

        try:
            if muted:
                logger.info("mic switch → muting")
                mute_mic()
            else:
                logger.info("mic switch → unmuting")
                unmute_mic()
        except Exception as e:
            logger.warning("Mic switch apply failed: %s", e)
