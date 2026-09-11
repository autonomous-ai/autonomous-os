"""Optional MPR121 capacitive input using shared GPIO button gestures.

I2C transport and register setup adapted from the user-supplied
mpr121_opi_test.py. Only the device-declared bus/address is accessed.
"""

import ctypes
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

from hal.board.mpr121 import MPR121Config
from hal.drivers.button_gestures import (
    DOUBLE_CLICK_WINDOW, FACTORY_RESET_DURATION, LONG_PRESS_DURATION,
    SLEEP_HOLD_DURATION,
)

logger = logging.getLogger(__name__)


class _I2CMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16), ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class _I2CData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_I2CMsg)), ("nmsgs", ctypes.c_uint32)]


class I2CBus:
    """Linux repeated-start I2C transport; importing this module needs no hardware."""

    def __init__(self, bus):
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        self._libc.ioctl.restype = ctypes.c_int
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR | os.O_CLOEXEC)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def _transfer(self, messages):
        array = (_I2CMsg * len(messages))(*messages)
        data = _I2CData(msgs=array, nmsgs=len(messages))
        result = self._libc.ioctl(self.fd, 0x0707, ctypes.byref(data))
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if result != len(messages):
            raise OSError("Incomplete MPR121 I2C transfer")

    def read_regs(self, address, register, length):
        pointer = (ctypes.c_uint8 * 1)(register)
        buffer = (ctypes.c_uint8 * length)()
        self._transfer([
            _I2CMsg(address, 0, 1, pointer),
            _I2CMsg(address, 1, length, buffer),
        ])
        return bytes(buffer)

    def write_reg(self, address, register, value):
        buffer = (ctypes.c_uint8 * 2)(register, value)
        self._transfer([_I2CMsg(address, 0, 2, buffer)])


@dataclass(frozen=True)
class _GestureEvent:
    kind: str
    gesture_id: int
    count: int = 0
    held_s: float = 0.0


class _GestureRecognizer:
    """Poll-clock recognition independent of action execution and electrode count."""

    def __init__(self, debounce_ms):
        self._delay = debounce_ms / 1000
        self._stable = None
        self._candidate = None
        self._since = 0.0
        self._armed = False
        self._press_start = None
        self._click_count = 0
        self._deadline = None
        self._gesture_id = 0
        self._hold_tier = 0

    def cancel(self):
        self._press_start = None
        self._click_count = 0
        self._deadline = None
        self._armed = False

    def update(self, touched, now):
        events = []
        if self._stable is None:
            self._stable = self._candidate = touched
            self._since = now
            self._armed = not touched
            logger.info("MPR121 event=detector_seed touched=%s armed=%s startup_hold_suppressed=%s", touched, self._armed, touched)
            return events
        if touched != self._candidate:
            self._candidate = touched
            self._since = now
            logger.debug("MPR121 event=debounce_candidate touched=%s", touched)
            if touched:
                # Cancel stale queued outcomes as soon as a new touch appears,
                # but never use raw chatter to increment the gesture count.
                events.append(_GestureEvent("invalidate", self._gesture_id))
        if touched != self._stable and now - self._since >= self._delay:
            self._stable = touched
            if touched and self._armed:
                if self._deadline is not None and self._since >= self._deadline:
                    logger.info("MPR121 event=burst_cancelled gesture_id=%d count=%d reason=new_touch_after_deadline", self._gesture_id, self._click_count)
                    self._click_count = 0
                    self._deadline = None
                if not self._click_count:
                    self._gesture_id += 1
                self._press_start = self._since
                self._hold_tier = 0
                events.append(_GestureEvent("press", self._gesture_id, self._click_count))
            elif not touched:
                if self._press_start is None:
                    logger.info("MPR121 event=stable_release startup_hold_suppressed=true")
                else:
                    # Measure between the first samples of the accepted edges;
                    # debounce must not push a just-short hold over a threshold.
                    held = self._since - self._press_start
                    events.append(_GestureEvent("release", self._gesture_id, self._click_count, held))
                    if held >= SLEEP_HOLD_DURATION:
                        self._click_count = 0
                        self._deadline = None
                        events.append(_GestureEvent("invalidate", self._gesture_id))
                        events.append(_GestureEvent("hold", self._gesture_id, held_s=held))
                    else:
                        self._click_count += 1
                        if self._click_count == 1:
                            events.append(_GestureEvent("single", self._gesture_id, self._click_count, held))
                        self._deadline = now + DOUBLE_CLICK_WINDOW
                    self._press_start = None
                self._armed = True
        if self._press_start is not None:
            held = (now if touched else self._since) - self._press_start
            tier = sum(held >= threshold for threshold in (
                SLEEP_HOLD_DURATION, LONG_PRESS_DURATION, FACTORY_RESET_DURATION,
            ))
            if tier > self._hold_tier:
                self._hold_tier = tier
                events.append(_GestureEvent("hold_tier", self._gesture_id, tier, held))
        # A pending click window never commits a destructive outcome while
        # another electrode is held; a completed hold clears the whole burst.
        if not touched and not self._stable and self._deadline is not None and now >= self._deadline:
            kind = "triple" if self._click_count == 3 else "cue"
            events.append(_GestureEvent(kind, self._gesture_id, self._click_count))
            self._click_count = 0
            self._deadline = None
        return events


def single_click_action(*, source, announce):
    from hal.drivers.button_actions import single_click_action as action
    action(source=source, announce=announce)


def announce_listening_cue(*, source):
    from hal.drivers.button_actions import announce_listening_cue as action
    action(source=source)


def triple_click_action(*, source):
    from hal.drivers.button_actions import triple_click_action as action
    action(source=source)


def hold_release_action(held_s, *, source):
    from hal.drivers.button_actions import hold_release_action as action
    action(held_s, source=source)


class MPR121Handler:
    def __init__(self, config: MPR121Config):
        self._config = config
        self._mask = sum(1 << electrode for electrode in set(config.electrodes))
        self._bus = None
        self._stop = threading.Event()
        self._pending = queue.Queue(maxsize=2)
        self._poll_thread = None
        self._action_thread = None
        self._detector = _GestureRecognizer(config.debounce_ms)
        self._last_raw_mask = None
        self._generation = 0
        self._action_busy = False
        self._gesture_lock = threading.Lock()
        self._hold_led = None

    def _feedback(self):
        with self._gesture_lock:
            if self._hold_led is None:
                from hal.drivers.button_actions import HoldLEDFeedback

                self._hold_led = HoldLEDFeedback()
            if self._stop.is_set():
                self._hold_led.stop()
            return self._hold_led

    def _initialize(self):
        config = self._config
        bus = self._bus

        def write(register, value):
            bus.write_reg(config.address, register, value)

        write(0x80, 0x63)
        time.sleep(0.001)
        write(0x5E, 0x00)
        if bus.read_regs(config.address, 0x5D, 1) != b"\x24":
            raise RuntimeError("MPR121 CONFIG2 reset value is not 0x24")
        for electrode in range(12):
            write(0x41 + 2 * electrode, config.touch_threshold)
            write(0x42 + 2 * electrode, config.release_threshold)
        for register, value in (
            (0x2B, 1), (0x2C, 1), (0x2D, 14), (0x2E, 0),
            (0x2F, 1), (0x30, 5), (0x31, 1), (0x32, 0),
            (0x33, 0), (0x34, 0), (0x35, 0),
            (0x5B, 0), (0x5C, 0x10), (0x5D, 0x20),
        ):
            write(register, value)
        if config.autoconfig:
            for register, value in ((0x7D, 200), (0x7F, 180), (0x7E, 130), (0x7B, 0x0B)):
                write(register, value)
        write(0x5E, 0x8F)

    def _read_touched(self):
        data = self._bus.read_regs(self._config.address, 0x00, 2)
        if len(data) != 2:
            raise OSError("Incomplete MPR121 touch status")
        status = int.from_bytes(data, "little")
        if status & 0x8000:
            raise OSError("MPR121 over-current fault")
        raw_mask = status & 0x0FFF
        if raw_mask != self._last_raw_mask:
            previous = self._last_raw_mask or 0
            touched = [i for i in range(12) if raw_mask & ~previous & (1 << i)]
            released = [i for i in range(12) if previous & ~raw_mask & (1 << i)]
            logger.info(
                "MPR121 event=electrodes initial=%s raw_mask=0x%03x selected_mask=0x%03x selected_active=0x%03x touched=%s released=%s",
                self._last_raw_mask is None, raw_mask, self._mask,
                raw_mask & self._mask, touched, released,
            )
            self._last_raw_mask = raw_mask
        return bool(raw_mask & self._mask)

    def start(self):
        if any(thread and thread.is_alive() for thread in (self._poll_thread, self._action_thread)):
            raise RuntimeError("MPR121 handler already running or stopping")
        logger.info(
            "MPR121 event=start bus=%d address=0x%02x electrodes=%s touch_threshold=%d release_threshold=%d autoconfig=%s poll_ms=%d debounce_ms=%d settle_ms=100 pending_capacity=2",
            self._config.bus, self._config.address, self._config.electrodes,
            self._config.touch_threshold, self._config.release_threshold,
            self._config.autoconfig, self._config.poll_ms, self._config.debounce_ms,
        )
        self._last_raw_mask = None
        if self._hold_led is not None:
            self._hold_led.stop()
        self._hold_led = None
        self._stop.clear()
        self._pending = queue.Queue(maxsize=2)
        self._detector = _GestureRecognizer(self._config.debounce_ms)
        try:
            self._bus = I2CBus(self._config.bus)
            self._initialize()
            # Allow conversions/autoconfiguration to settle before seeding the
            # boot-held suppression from the first reported electrode state.
            time.sleep(0.1)
            self._detector.update(self._read_touched(), time.monotonic())
            self._action_thread = threading.Thread(target=self._dispatch, daemon=True, name="mpr121-actions")
            self._poll_thread = threading.Thread(target=self._poll, daemon=True, name="mpr121-poll")
            self._action_thread.start()
            self._poll_thread.start()
        except Exception:
            logger.exception("MPR121 event=start_failed")
            self._stop.set()
            if self._hold_led is not None:
                self._hold_led.stop()
            self._close_bus()
            raise
        logger.info("MPR121 ready on i2c-%d address 0x%02x electrodes %s", self._config.bus, self._config.address, self._config.electrodes)

    def _close_bus(self):
        if self._bus is not None:
            bus, self._bus = self._bus, None
            try:
                bus.close()
                logger.info("MPR121 event=bus_closed bus=%d", self._config.bus)
            except OSError:
                logger.exception("MPR121 bus close failed")

    def _invalidate_pending(self, reason):
        with self._gesture_lock:
            self._generation += 1
            while True:
                try:
                    _, event, _ = self._pending.get_nowait()
                except queue.Empty:
                    break
                logger.info("MPR121 event=action_discarded gesture_id=%d action=%s reason=%s", event.gesture_id, event.kind, reason)

    def _process_touch(self, touched, now):
        for event in self._detector.update(touched, now):
            if self._stop.is_set():
                return
            logger.info("MPR121 event=gesture kind=%s gesture_id=%d count=%d held_s=%.3f", event.kind, event.gesture_id, event.count, event.held_s)
            if event.kind == "invalidate":
                self._invalidate_pending("new_touch_or_hold")
            elif event.kind == "hold_tier":
                self._feedback().set_tier(event.count)
            elif event.kind == "release":
                if self._hold_led is not None:
                    self._hold_led.release()
            elif event.kind in ("single", "cue", "triple", "hold"):
                with self._gesture_lock:
                    if self._stop.is_set():
                        return
                    if event.kind in ("hold", "triple") and (self._action_busy or not self._pending.empty()):
                        logger.warning("MPR121 event=action_discarded gesture_id=%d action=%s reason=action_worker_busy", event.gesture_id, event.kind)
                        continue
                    try:
                        self._pending.put_nowait((self._generation, event, time.monotonic()))
                        logger.info("MPR121 event=action_queued gesture_id=%d action=%s", event.gesture_id, event.kind)
                    except queue.Full:
                        # Counts are resolved from every electrode edge above;
                        # dropping a semantic outcome never invents a triple.
                        logger.warning("MPR121 event=action_discarded gesture_id=%d action=%s reason=pending_queue_full", event.gesture_id, event.kind)

    def _poll(self):
        logger.info("MPR121 event=worker_started worker=poll")
        try:
            while not self._stop.wait(self._config.poll_ms / 1000):
                self._process_touch(self._read_touched(), time.monotonic())
        except Exception:
            logger.exception("MPR121 polling stopped after hardware error")
            self._stop.set()
        finally:
            self._detector.cancel()
            if self._hold_led is not None:
                self._hold_led.stop()
            self._invalidate_pending("poll_stopped")
            self._close_bus()
            logger.info("MPR121 event=worker_stopped worker=poll")

    def _execute(self, event):
        if event.kind == "single":
            single_click_action(source="MPR121", announce=False)
        elif event.kind == "cue":
            announce_listening_cue(source="MPR121")
        elif event.kind == "triple":
            triple_click_action(source="MPR121")
        elif event.kind == "hold":
            if self._feedback().commit(event.held_s) is False:
                logger.info("MPR121 event=action_discarded gesture_id=%d action=hold reason=feedback_cancelled", event.gesture_id)
                return
            hold_release_action(event.held_s, source="MPR121")

    def _dispatch(self):
        logger.info("MPR121 event=worker_started worker=action")
        try:
            while not self._stop.is_set():
                try:
                    generation, event, queued_at = self._pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                with self._gesture_lock:
                    valid = not self._stop.is_set() and generation == self._generation
                    if event.kind in ("hold", "triple") and time.monotonic() - queued_at > DOUBLE_CLICK_WINDOW:
                        valid = False
                    if valid:
                        self._action_busy = True
                if not valid:
                    logger.info("MPR121 event=action_discarded gesture_id=%d action=%s reason=stale_expired_or_stopping", event.gesture_id, event.kind)
                    continue
                try:
                    logger.info("MPR121 event=action_begin gesture_id=%d action=%s count=%d held_s=%.3f", event.gesture_id, event.kind, event.count, event.held_s)
                    # Once a shared action starts its own I/O/OS sequence, it
                    # cannot be interrupted here. Only pending work is canceled.
                    self._execute(event)
                    logger.info("MPR121 event=action_complete gesture_id=%d action=%s", event.gesture_id, event.kind)
                except Exception:
                    logger.exception("MPR121 event=action_failed gesture_id=%d action=%s", event.gesture_id, event.kind)
                finally:
                    with self._gesture_lock:
                        self._action_busy = False
        finally:
            self._invalidate_pending("action_worker_stopped")
            logger.info("MPR121 event=worker_stopped worker=action")

    def stop(self):
        logger.info("MPR121 event=stop_requested")
        self._stop.set()
        if self._hold_led is not None:
            self._hold_led.stop()
        self._invalidate_pending("stop_requested")
        for thread in (self._poll_thread, self._action_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    logger.warning("MPR121 worker %s still stopping", thread.name)
        # The polling thread owns the bus after start, so never close its fd
        # underneath an in-flight ioctl. Its finally block closes it on exit.
