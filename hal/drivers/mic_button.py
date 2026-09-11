"""Configured level-driven mic-mute slide switch.

Wiring and timing are injected from the device configuration. Boot synchronizes
mic state with the switch; edges reconcile after settling. The watchdog only
reconciles changed physical levels so software-only mute remains untouched.
"""

import logging
import threading
import time

import hal.app_state as state
from hal.board.mic_button import MicButtonConfig

logger = logging.getLogger(__name__)


class MicButtonHandler:
    def __init__(self, config: MicButtonConfig | None):
        self._config = config
        self._stopped = threading.Event()
        self._stopped.set()
        self._watchdog_thread: threading.Thread | None = None
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
        # Latency-trace: monotonic ts of the last GPIO edge; reset every
        # _on_edge fire. Feeds the [mic-switch-trace] logs so operators can
        # tell "switch was slow" from "internal ops were slow" at a glance.
        self._last_edge_ts: float = 0.0
        # Last pin level we actually applied to app_state. Set at boot-sync
        # and every reconcile. The watchdog compares the CURRENT pin against
        # this — a divergence means the lgpio callback thread missed an edge
        # (silent stall) and we need to catch up. WITHOUT this compare the
        # watchdog would blindly re-drive state to match the pin every 30s,
        # which reverts software-only mutes (web UI / voice command) because
        # the pin never moved. None until start() populates it.
        self._last_known_level: int | None = None

    def start(self):
        if self._config is None:
            logger.info("Mic switch disabled: no device wiring configured")
            return
        if not self._stopped.is_set():
            return
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            logger.warning("Mic switch start skipped: previous watchdog is still stopping")
            return

        import lgpio

        self._lgpio = lgpio
        self._stopped.clear()

        try:
            self._handle = lgpio.gpiochip_open(self._config.chip)
        except Exception as e:
            logger.warning("Mic switch gpiochip_open(%d) failed: %s", self._config.chip, e)
            self.stop()
            return

        try:
            lgpio.gpio_claim_alert(
                self._handle, self._config.line, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
            )
            self._callback = lgpio.callback(
                self._handle, self._config.line, lgpio.BOTH_EDGES, self._on_edge
            )
        except Exception as e:
            logger.warning(
                "Mic switch claim line %d failed: %s -- disabled", self._config.line, e
            )
            self.stop()
            return

        # Sync boot-time state to the switch's current position. Without this,
        # a device that boots with the switch already in "muted" would run
        # with the mic hot until the user throws it once and back.
        try:
            initial_level = lgpio.gpio_read(self._handle, self._config.line)
        except Exception as e:
            logger.warning("Mic switch initial read failed: %s", e)
            initial_level = 1 - self._config.muted_level  # default to unmuted if read fails

        logger.info(
            "Mic mute switch ready on gpiochip%d line %d (initial level=%d, settle %d ms, watchdog %ds)",
            self._config.chip,
            self._config.line,
            initial_level,
            int(self._config.settle_s * 1000),
            int(self._config.watchdog_s),
        )

        # Boot-time sync runs SYNCHRONOUSLY (unlike the edge handler which
        # threads off) so subsequent HAL init phases see the final mic state.
        # Without this, a reboot with the switch already in "muted" would let
        # voice_service start briefly listening before the mute lands, opening
        # a ~hundreds-of-ms window where the mic is hot despite the hardware
        # kill switch being off. The route call is idempotent and cheap
        # (~tens of ms at most) so blocking start() here is worth it.
        self._last_known_level = initial_level
        with self._apply_lock:
            self._apply_state_locked(initial_level == self._config.muted_level)

        # Watchdog: periodic pin re-read + reconcile. Self-heals if the
        # lgpio edge-callback thread stalls silently. Daemon so it dies with
        # the process; stop() also cancels and joins the watchdog.
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="mic-switch-watchdog"
        )
        self._watchdog_thread.start()

    def stop(self):
        """Cancel asynchronous work before releasing the GPIO handle."""
        self._stopped.set()
        with self._timer_lock:
            if self._settle_timer is not None:
                self._settle_timer.cancel()
                self._settle_timer = None
        if self._callback is not None:
            try:
                self._callback.cancel()
            except Exception as exc:
                logger.warning("Mic switch callback cancel failed: %s", exc)
            finally:
                self._callback = None
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            if not self._watchdog_thread.is_alive():
                self._watchdog_thread = None
        with self._apply_lock:
            if self._handle is not None:
                try:
                    self._lgpio.gpiochip_close(self._handle)
                except Exception as exc:
                    logger.warning("Mic switch gpiochip_close failed: %s", exc)
                finally:
                    self._handle = None

    def _on_edge(self, chip, gpio, level, tick):
        # Restart the settle timer on every edge. The `level` param from
        # lgpio is deliberately IGNORED here — bouncing contacts can call
        # this several times with alternating levels within a few ms and
        # the last one is not always the terminal position. Instead we
        # re-read the pin in _reconcile after the bounce settles, so the
        # applied state matches the switch's real end position.
        self._last_edge_ts = time.monotonic()
        logger.info("[mic-switch-trace] EDGE fired (level=%d, tick=%d)", level, tick)
        with self._timer_lock:
            if self._stopped.is_set():
                return
            if self._settle_timer is not None:
                self._settle_timer.cancel()
            t = threading.Timer(self._config.settle_s, self._reconcile)
            t.daemon = True
            t.name = "mic-switch-settle"
            self._settle_timer = t
            t.start()

    def _reconcile(self):
        """Read the pin now and drive HAL state to match. Called from the
        settle Timer (after an edge storm quiets) and from the watchdog.
        Runs under _apply_lock so concurrent triggers can't race the
        underlying mute_mic() / unmute_mic() routes."""
        t_lock_want = time.monotonic()
        with self._apply_lock:
            if self._stopped.is_set():
                return
            logger.info(
                "[mic-switch-trace] APPLY_LOCK acquired (waited=%.0fms)",
                (time.monotonic() - t_lock_want) * 1000,
            )
            self._reconcile_locked()

    def _reconcile_locked(self):
        t_recon = time.monotonic()
        since_edge = (t_recon - self._last_edge_ts) if self._last_edge_ts else -1
        try:
            current_level = self._lgpio.gpio_read(self._handle, self._config.line)
        except Exception as e:
            logger.warning("Mic switch reconcile read failed: %s", e)
            return
        logger.info(
            "[mic-switch-trace] RECONCILE pin_level=%d muted=%s (settle_wait=%.0fms since last edge)",
            current_level,
            current_level == self._config.muted_level,
            since_edge * 1000 if since_edge >= 0 else -1,
        )
        self._apply_state_locked(current_level == self._config.muted_level)
        self._last_known_level = current_level
        logger.info(
            "[mic-switch-trace] APPLY_STATE done (total_edge→done=%.0fms)",
            (time.monotonic() - self._last_edge_ts) * 1000 if self._last_edge_ts else -1,
        )

    def _watchdog_loop(self):
        """Periodic pin re-read to catch missed edges (lgpio callback thread
        has been observed to stall silently under sustained edge storms).
        Compares the CURRENT pin against the last level we applied — reconciles
        ONLY on a divergence. Blindly forcing reconcile every tick would revert
        software-only mutes (web UI, voice command, MQTT) because the physical
        pin never moved from its idle position, but our state did — verified
        2026-07-24: user muted via web, watchdog auto-unmuted 28s later because
        pin_level=1 while state._mic_muted=True."""
        while not self._stopped.wait(self._config.watchdog_s):
            try:
                with self._apply_lock:
                    if self._stopped.is_set():
                        return
                    current = self._lgpio.gpio_read(self._handle, self._config.line)
            except Exception as e:
                logger.warning("Mic switch watchdog read failed: %s", e)
                continue
            if self._last_known_level is None or current == self._last_known_level:
                # Pin hasn't moved since last apply — callback (if fired) hasn't
                # missed anything. Software may have mutated state._mic_muted
                # via another channel; leave it alone.
                continue
            logger.warning(
                "Mic switch watchdog: pin state diverged (pin=%d, last_known=%d) — callback likely stalled, reconciling",
                current, self._last_known_level,
            )
            try:
                self._reconcile()
            except Exception as e:
                logger.warning("Mic switch watchdog reconcile failed: %s", e)

    def _paint_listening_after_cue(self):
        """Fire the blue LISTENING pulse after the unmute "I'm listening"
        TTS cue finishes so the strip settles on a clear "ready to listen"
        cue instead of the warm-white last-frame from the TTS wave.

        Poll _tts_speaking (up to 5s) rather than sleeping a fixed delay:
        the cached clip is ~740ms but launch latency varies (thread
        startup + first-audio jitter), and a fixed 1.5s wait was landing
        WHILE the wave was still painting → _apply_emotion_led_display's
        "Emotion LED skipped -- TTS speaking_wave active" guard swallowed
        the paint and blue never appeared. Also bail if the user re-muted
        or music started meanwhile."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._stopped.is_set() or state._hw_mic_switch_muted is True or state._mic_muted:
                return
            if state._music_playing:
                return
            if not state._tts_speaking:
                break
            if self._stopped.wait(0.1):
                return
        else:
            logger.info("mic switch unmute listening cue: TTS never quiesced -- skipping")
            return
        # Small settle after TTS end so the wave's teardown restore lands
        # first; painting into a live restore path races the effect thread.
        if self._stopped.wait(0.15):
            return
        if state._hw_mic_switch_muted is True or state._mic_muted:
            return
        try:
            from hal.presets import EMO_LISTENING

            state._apply_emotion_led_display(EMO_LISTENING, 0.7, force_led=True)
            logger.info("mic switch unmute -- painted LISTENING cue on strip")
        except Exception as e:
            logger.warning("mic switch unmute listening cue failed: %s", e)

    def _apply_state_locked(self, muted: bool):
        """Push mic state to match the switch position. Idempotent — if HAL
        state already matches (web admin just set it, or start-up sync
        found the correct value), skip the route call to avoid log spam
        and needless voice-service restarts.

        MUST be called with _apply_lock held (or from single-threaded
        contexts like boot init) so the check-then-write on state._mic_muted
        isn't racy against another edge/watchdog reconcile.
        """
        # Publish the physical switch position BEFORE the idempotency skip:
        # /voice/status and single_click_action guards read this flag, so a
        # boot-time sync where the sidecar already agrees with the switch
        # still needs to advertise "HW-locked" to the web.
        state._hw_mic_switch_muted = muted

        if state._mic_muted == muted:
            return

        from hal.routes.voice import mute_mic, stop_tts

        try:
            if muted:
                logger.info("mic switch → muting")
                # Order matters. Do the fast quiet-me ops FIRST (stop_tts,
                # audio_stop each ~10ms) BEFORE painting red, because
                # audio_stop unconditionally invokes _on_music_complete
                # which, when the user has no saved LED state, tramples the
                # current effect and starts its own "idle breathing"
                # fallback (routes/music.py) — that would kill the red the
                # instant we painted it. Painting AFTER means the red is
                # the LAST effect started, so nothing races it. mute_mic
                # (slow, seconds — voice_service.stop() session teardown)
                # still runs last so the visual feedback is not gated on it.
                stop_tts()
                from hal.routes.music import audio_stop

                audio_stop()
                # Now paint. force also overlays a live TTS/music wave, so
                # even a mid-utterance throw gets immediate red feedback.
                state._apply_mic_muted_led(force=True)
                mute_mic()
            else:
                logger.info("mic switch → unmuting")
                t0 = time.monotonic()
                # Symmetric: never leave the red showing while the mic is
                # hot — kill it immediately, even mid-wave.
                state._clear_mic_muted_led(force=True)
                logger.info("[mic-switch-trace] unmute step1 clear_red done +%.0fms", (time.monotonic() - t0) * 1000)
                # Reuse the 1-tap action instead of a bare unmute_mic():
                # wake-if-sleepy + relax speaker mute + unmute mic + ack
                # chime + localized "I'm listening" cue — the slide switch
                # gets the same feedback set as the button/touchpad.
                from hal.drivers.button_actions import single_click_action

                t_sca = time.monotonic()
                single_click_action("mic-switch")
                logger.info("[mic-switch-trace] unmute step2 single_click_action done +%.0fms (cumul=%.0fms)", (time.monotonic() - t_sca) * 1000, (time.monotonic() - t0) * 1000)
                # After unmute the natural resting look is warm-white
                # ambient because _user_led_state is None on fresh boots —
                # the "listening" blue pulse only fires when speech is
                # actually detected (voice_service.py). Users read that
                # gap as "did unmute even work?" Paint the blue listening
                # indicator NOW so the transition red → blue is instant.
                # Delayed ~1.5s so the "I'm listening" TTS cue (~1s
                # cached clip) finishes first — otherwise its speaking-
                # wave overlays and immediately hides the blue. force_led
                # skips the background-emotion "respect user state" guard
                # (LISTENING is a background emotion).
                threading.Thread(
                    target=self._paint_listening_after_cue,
                    daemon=True,
                    name="mic-switch-listening-cue",
                ).start()
        except Exception as e:
            logger.warning("Mic switch apply failed: %s", e)
