"""GPIO button handler with device-declared wiring.

Supports five actions on a single button:
- Single click: stop speaker / unmute mic (fires immediately on release)
- Triple click: reboot OS (resolved after the click window)
- Hold + release (2–5s):   sleepy emotion
- Hold + release (5–10s):  shutdown OS
- Hold + release (10s+):   factory-reset (wipe state, reboot to AP setup)

Destructive actions commit ON RELEASE, not on a timer firing while held,
so the user can cancel mid-hold by releasing before crossing a threshold
(or keep holding past 10s to escalate from shutdown → factory-reset).

The silent part of the single-click action (stop speaker / unmute mic)
fires on the FIRST tap of a burst without waiting for the click window —
it's non-destructive, so eager firing just cuts barge-in latency. The
audible listening cue waits for the window to resolve so it never talks
over a triple-click in progress. Double-click and 4+ rapid clicks add
nothing on top of the floor-grab — destructive actions (reboot/shutdown/
factory-reset) need a deliberate gesture so a user panic-clicking the
button to interrupt TTS doesn't accidentally reboot.

The actual action logic lives in `button_actions.py` so other input
devices (touchpad, remote) can reuse the same gestures.
"""

import logging
import threading
import time

from hal.board.gpio_button import ButtonConfig
from hal.drivers.button_actions import (
    HoldLEDFeedback,
    DOUBLE_CLICK_WINDOW,
    FACTORY_RESET_DURATION,
    LONG_PRESS_DURATION,
    SLEEP_HOLD_DURATION,
    announce_listening_cue,
    hold_release_action,
    single_click_action,
    triple_click_action,
)

logger = logging.getLogger(__name__)

class GPIOButtonHandler:
    def __init__(self, config: ButtonConfig):
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
        self._hold_lock = threading.Lock()
        self._hold_led = HoldLEDFeedback()
        self._chip = config.chip
        self._pin = config.line
        self._debounce_ns = config.debounce_ns
        self._last_press_tick = 0
        self._last_release_tick = 0

    def _hold_watcher(self, stop_event):
        """Report hold tiers; shared feedback owns all LED animation."""
        last_stage = 0
        while not stop_event.is_set():
            with self._hold_lock:
                if stop_event.is_set() or self._hold_watcher_stop is not stop_event:
                    return
                held = time.monotonic() - self._press_start
                stage = (3 if held >= FACTORY_RESET_DURATION else
                         2 if held >= LONG_PRESS_DURATION else
                         1 if held >= SLEEP_HOLD_DURATION else 0)
                if stage != last_stage:
                    self._hold_led.set_tier(stage)
                    last_stage = stage
            if stop_event.wait(timeout=0.1):
                return

    def _run_hold_action(self, held):
        if self._hold_led.commit(held) is False:
            return
        action = "factory-reset" if held >= FACTORY_RESET_DURATION else "shutdown" if held >= LONG_PRESS_DURATION else "sleepy"
        logger.info("GPIO button hold %.1fs -- %s", held, action)
        hold_release_action(held, source="GPIO button")

    def start(self):
        import lgpio

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
            with self._hold_lock:
                self._press_start = time.monotonic()
                self._pressed = True
                if self._hold_watcher_stop is not None:
                    self._hold_watcher_stop.set()
                self._hold_led.release()
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
        # Cancel blinking without blocking the GPIO callback on RGB I/O.
        with self._hold_lock:
            if self._hold_watcher_stop is not None:
                self._hold_watcher_stop.set()
                self._hold_watcher_stop = None
            self._hold_led.release()

        held = time.monotonic() - self._press_start
        if held >= SLEEP_HOLD_DURATION:
            self._click_count = 0  # destructive, terminal: scrub any pending clicks
            if self._click_timer:
                self._click_timer.cancel()
                self._click_timer = None

            # Edge handling only supplies the released-duration signal. The
            # action library owns which semantic action that duration selects.
            # It may wait for a cue or release servos, so keep the GPIO callback
            # short and never block subsequent hardware edges.
            threading.Thread(
                target=self._run_hold_action,
                args=(held,),
                daemon=True,
                name="gpio-button-hold-action",
            ).start()
            return

        # Short tap → count toward triple-click resolution. The SILENT part
        # of the single-click action (stop speaker / unmute mic) fires
        # IMMEDIATELY on the first tap of a burst — it's a non-destructive
        # "give me the floor" gesture, so firing now cuts perceived barge-in
        # latency by 0.4s. The audible listening cue is deferred until the
        # click window resolves: a cue talking over the user mid-triple-click
        # disrupts their rhythm, and a resolved triple should only speak the
        # reboot announce. If the burst turns out to be a triple, the silent
        # floor-grab side effects are harmless — the OS reboots moments later.
        self._click_count += 1
        if self._click_count == 1:
            # Off-thread: stop_tts/audio_stop/unmute do blocking I/O; the
            # lgpio callback must return promptly (same reasoning as the
            # long-press branches above).
            threading.Thread(
                target=single_click_action,
                kwargs={"source": "GPIO button", "announce": False},
                daemon=True,
                name="gpio-button-single-click",
            ).start()
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
        if count == 3:
            triple_click_action(source="GPIO button")
            return
        if count != 1:
            # count == 2 → likely a slipped/panic double-tap of single
            # count >= 4 → panic-click; never trigger destructive actions
            logger.info("GPIO button %d clicks -- ignored (only 1=stop, 3=reboot)", count)
        # Any non-triple burst already grabbed the floor silently on its
        # first tap — now that it's resolved, speak the deferred cue so the
        # user hears confirmation exactly once per burst.
        announce_listening_cue(source="GPIO button")
