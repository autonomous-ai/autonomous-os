"""GPIO button handler for pin 17.

Supports four actions on a single button:
- Single click: stop speaker / unmute mic
- Triple click: reboot OS
- Hold + release (5–10s):  shutdown OS
- Hold + release (10s+):   factory-reset (wipe state, reboot to AP setup)

Destructive actions commit ON RELEASE, not on a timer firing while held,
so the user can cancel mid-hold by releasing before crossing a threshold
(or keep holding past 10s to escalate from shutdown → factory-reset).

Double-click and 4+ rapid clicks are intentional no-ops — destructive
actions (reboot/shutdown/factory-reset) need a deliberate gesture so a
user panic-clicking the button to interrupt TTS doesn't accidentally
reboot.

The actual action logic lives in `button_actions.py` so other input
devices (touchpad, remote) can reuse the same gestures.
"""

import logging
import threading
import time

import lelamp.app_state as state
from lelamp.presets import RGB_CMD_SOLID
from lelamp.platform.board import board_profile
from lelamp.service.base import Priority
from lelamp.service.button_actions import (
    DOUBLE_CLICK_WINDOW,
    FACTORY_RESET_DURATION,
    LONG_PRESS_DURATION,
    factory_reset_action,
    long_press_action,
    single_click_action,
    triple_click_action,
)

logger = logging.getLogger(__name__)

# LED feedback during hold (Tier B design from the factory-reset discussion).
# Amber pulse at 5–10s tells the user "shutdown is armed — releasing now
# commits". Red solid at 10s+ tells them they've escalated to factory-reset.
# Both dispatch at HIGH priority so they preempt the current emotion LED.
LED_SHUTDOWN_WARN = (255, 165, 0)   # amber
LED_FACTORY_RESET = (255, 0, 0)     # red
LED_OFF = (0, 0, 0)
# Pulse: 0.5 s on + 0.5 s off = 1 Hz full cycle.
LED_PULSE_HALF_PERIOD_S = 0.5

# Per-board button wiring (chip / line / debounce_ns) lives in the board
# platform layer — os/hal/lelamp/platform/board.py — the single source of truth
# shared across drivers. lgpio.callback tick is nanoseconds; both per-board
# debounce values stay well under DOUBLE_CLICK_WINDOW so triple click is
# still detectable.


def _resolve_board_config() -> tuple[int, int, int]:
    """Return (chip, line, debounce_ns) for the wake button on this board."""
    b = board_profile().button
    return b.chip, b.line, b.debounce_ns


class GPIOButtonHandler:
    def __init__(self):
        self._lgpio = None
        self._handle = None
        self._callback = None
        self._click_count = 0
        self._click_timer = None
        self._press_start = 0
        # Track whether we've seen the press edge so a stray release edge
        # (debounce-dropped press) doesn't fire stale held-duration actions.
        self._pressed = False
        # Hold-duration LED watcher. Each press creates a new threading.Event
        # (per-watcher stop) so the previous watcher exits cleanly without
        # racing the new one. None when no hold is active.
        self._hold_watcher_stop = None
        self._chip = 0
        self._pin = 0
        self._debounce_ns = PI_DEBOUNCE_NS
        self._last_press_tick = 0
        self._last_release_tick = 0

    def _dispatch_led(self, color):
        """Push a solid color to RGB service at HIGH priority so it preempts
        the current emotion LED. Silent no-op when RGB service unavailable
        (dev machines, hardware issues — button still works)."""
        rgb = state.rgb_service
        if rgb is None:
            return
        try:
            rgb.dispatch(RGB_CMD_SOLID, color, priority=Priority.HIGH)
        except Exception as e:
            logger.warning("LED dispatch failed: %s", e)

    def _hold_watcher(self, stop_event):
        """Poll hold duration and update LED at threshold crossings. One
        watcher thread per press — release sets stop_event and a new press
        starts a fresh watcher with a new Event so the two never race."""
        last_stage = -1
        pulse_on = False
        while not stop_event.is_set():
            held = time.monotonic() - self._press_start
            if held >= FACTORY_RESET_DURATION:
                stage = 2  # red solid — armed for factory-reset
            elif held >= LONG_PRESS_DURATION:
                stage = 1  # amber pulse 1 Hz — armed for shutdown
            else:
                stage = 0  # quiet — under shutdown threshold

            # Stage 2 entry: set red solid once (no pulse). Subsequent loops
            # leave it alone so the LED doesn't flicker.
            if stage != last_stage and stage == 2:
                self._dispatch_led(LED_FACTORY_RESET)
            last_stage = stage

            if stage == 1:
                # Half-period toggle gives a 1 Hz pulse (0.5 s on, 0.5 s off).
                pulse_on = not pulse_on
                self._dispatch_led(LED_SHUTDOWN_WARN if pulse_on else LED_OFF)
                wait = LED_PULSE_HALF_PERIOD_S
            else:
                wait = 0.1

            if stop_event.wait(timeout=wait):
                return

    def start(self):
        import lgpio

        self._chip, self._pin, self._debounce_ns = _resolve_board_config()
        self._lgpio = lgpio
        self._handle = lgpio.gpiochip_open(self._chip)
        lgpio.gpio_claim_alert(
            self._handle, self._pin, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
        )
        self._callback = lgpio.callback(
            self._handle, self._pin, lgpio.BOTH_EDGES, self._on_edge
        )
        logger.info(
            "GPIO button ready on gpiochip%d line %d (manual debounce %d ms)",
            self._chip,
            self._pin,
            self._debounce_ns // 1_000_000,
        )

    def _on_edge(self, chip, gpio, level, tick):
        # Per-edge debounce. Track press/release ticks independently so a
        # quick click (rising edge soon after the falling edge) isn't
        # dropped, while bouncy repeats of the same edge are filtered out.
        # OrangePi's gpiochip1 reports more contact bounce than the Pi.
        if level == 0:
            if tick - self._last_press_tick < self._debounce_ns:
                return
            self._last_press_tick = tick
        else:
            if tick - self._last_release_tick < self._debounce_ns:
                return
            self._last_release_tick = tick

        if level == 0:
            # Button pressed (falling edge). All destructive actions commit
            # on release based on hold duration — no timer fires while held,
            # so the user can always cancel by releasing before the next
            # threshold (or escalate from shutdown → factory-reset by
            # holding past 10s). LED feedback runs in a watcher thread.
            self._press_start = time.monotonic()
            self._pressed = True
            # Signal any leftover watcher (shouldn't exist due to release
            # cleanup, defensive) then start a fresh one with a new Event.
            if self._hold_watcher_stop is not None:
                self._hold_watcher_stop.set()
            new_stop = threading.Event()
            self._hold_watcher_stop = new_stop
            threading.Thread(
                target=self._hold_watcher,
                args=(new_stop,),
                daemon=True,
                name="gpio-button-hold-led",
            ).start()
            return

        # Button released (rising edge).
        if not self._pressed:
            # Stale release edge (matching press was debounce-dropped).
            # _press_start may be from minutes ago — refusing to act is
            # safer than firing a destructive action against stale state.
            logger.warning("GPIO button release without matching press -- ignoring")
            return
        self._pressed = False
        # Stop LED watcher. Watcher exits within its current sleep (< 0.5 s).
        # LED stays at whatever colour it last dispatched — destructive
        # branches below reaffirm with a solid colour so it doesn't freeze
        # mid-pulse.
        if self._hold_watcher_stop is not None:
            self._hold_watcher_stop.set()
            self._hold_watcher_stop = None

        held = time.monotonic() - self._press_start
        if held >= FACTORY_RESET_DURATION:
            logger.info("GPIO button hold %.1fs -- factory-reset", held)
            self._click_count = 0  # destructive, terminal: scrub any pending clicks
            if self._click_timer:
                self._click_timer.cancel()
                self._click_timer = None
            # Lock the LED at red solid until reboot kills us. Watcher already
            # set it but reaffirm (idempotent at HIGH priority).
            self._dispatch_led(LED_FACTORY_RESET)
            # Off-thread: factory_reset_action blocks ~3s (TTS announce) +
            # servo release + HTTP POST. lgpio callback must return promptly
            # or subsequent edges queue up. Original `_on_long_press` ran in
            # a Timer thread; we preserve that property here.
            threading.Thread(
                target=factory_reset_action,
                kwargs={"source": "GPIO button"},
                daemon=True,
                name="gpio-button-factory-reset",
            ).start()
            return
        if held >= LONG_PRESS_DURATION:
            logger.info("GPIO button hold %.1fs -- shutdown", held)
            self._click_count = 0
            if self._click_timer:
                self._click_timer.cancel()
                self._click_timer = None
            # Freeze LED at amber solid (was pulsing). Confirms the gesture
            # committed to shutdown, stays on through the 5 s TTS announce.
            self._dispatch_led(LED_SHUTDOWN_WARN)
            # Off-thread: same reasoning as factory-reset above (announce +
            # 5s sleep + servo release + subprocess.Popen shutdown).
            threading.Thread(
                target=long_press_action,
                kwargs={"source": "GPIO button"},
                daemon=True,
                name="gpio-button-long-press",
            ).start()
            return

        # Short tap → count toward single / triple click resolution.
        self._click_count += 1
        if self._click_timer:
            self._click_timer.cancel()
        self._click_timer = threading.Timer(
            DOUBLE_CLICK_WINDOW, self._on_click_timeout
        )
        self._click_timer.daemon = True
        self._click_timer.start()

    def _on_click_timeout(self):
        count = self._click_count
        self._click_count = 0
        if count == 1:
            single_click_action(source="GPIO button")
        elif count == 3:
            triple_click_action(source="GPIO button")
        else:
            # count == 2 → likely a slipped/panic double-tap of single
            # count >= 4 → panic-click; never trigger destructive actions
            logger.info("GPIO button %d clicks -- ignored (only 1=stop, 3=reboot)", count)
