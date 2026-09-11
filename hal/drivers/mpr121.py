"""Optional MPR121 capacitive input: a complete touch maps to single click.

I2C transport and register setup adapted from the user-supplied
mpr121_opi_test.py. Only the device-declared bus/address is accessed.
"""

import ctypes
import logging
import os
import queue
import threading
import time

from hal.board.mpr121 import MPR121Config

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


class _TapDetector:
    """Debounce the union of selected electrodes; ignore a boot-time hold."""

    def __init__(self, debounce_ms):
        self._delay = debounce_ms / 1000
        self._stable = None
        self._candidate = None
        self._since = 0.0
        self._armed = False
        self._pressed = False

    def update(self, touched, now):
        if self._stable is None:
            self._stable = self._candidate = touched
            self._since = now
            self._armed = not touched
            logger.info("MPR121 event=detector_seed touched=%s armed=%s startup_hold_suppressed=%s", touched, self._armed, touched)
            return False
        if touched != self._candidate:
            self._candidate = touched
            self._since = now
            logger.debug("MPR121 event=debounce_candidate touched=%s", touched)
        if touched == self._stable or now - self._since < self._delay:
            return False
        self._stable = touched
        if touched:
            self._pressed = self._armed
            logger.info("MPR121 event=stable_touch armed=%s", self._armed)
            return False
        clicked = self._pressed
        self._pressed = False
        self._armed = True
        logger.info("MPR121 event=stable_release tap_accepted=%s startup_hold_suppressed=%s", clicked, not clicked)
        return clicked


def single_click_action(*, source):
    # Keep platform/audio dependencies out of transport initialization.
    from hal.drivers.button_actions import single_click_action as action
    action(source=source)


class MPR121Handler:
    def __init__(self, config: MPR121Config):
        self._config = config
        self._mask = sum(1 << electrode for electrode in set(config.electrodes))
        self._bus = None
        self._stop = threading.Event()
        self._pending = queue.Queue(maxsize=1)
        self._poll_thread = None
        self._action_thread = None
        self._detector = _TapDetector(config.debounce_ms)
        self._last_raw_mask = None
        self._tap_id = 0

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
            "MPR121 event=start bus=%d address=0x%02x electrodes=%s touch_threshold=%d release_threshold=%d autoconfig=%s poll_ms=%d debounce_ms=%d settle_ms=100 pending_capacity=1",
            self._config.bus, self._config.address, self._config.electrodes,
            self._config.touch_threshold, self._config.release_threshold,
            self._config.autoconfig, self._config.poll_ms, self._config.debounce_ms,
        )
        self._last_raw_mask = None
        self._stop.clear()
        self._pending = queue.Queue(maxsize=1)
        self._detector = _TapDetector(self._config.debounce_ms)
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

    def _poll(self):
        logger.info("MPR121 event=worker_started worker=poll")
        try:
            while not self._stop.wait(self._config.poll_ms / 1000):
                if self._detector.update(self._read_touched(), time.monotonic()):
                    self._tap_id += 1
                    logger.info("MPR121 event=tap_accepted tap_id=%d", self._tap_id)
                    try:
                        self._pending.put_nowait(self._tap_id)
                        logger.info("MPR121 event=action_queued tap_id=%d", self._tap_id)
                    except queue.Full:
                        logger.info("MPR121 event=action_coalesced tap_id=%d reason=pending_queue_full", self._tap_id)
        except Exception:
            logger.exception("MPR121 polling stopped after hardware error")
            self._stop.set()
        finally:
            self._close_bus()
            logger.info("MPR121 event=worker_stopped worker=poll")

    def _dispatch(self):
        logger.info("MPR121 event=worker_started worker=action")
        try:
            while not self._stop.is_set():
                try:
                    tap_id = self._pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    logger.info("MPR121 event=action_discarded tap_id=%s reason=stopping", tap_id)
                    return
                try:
                    logger.info("MPR121 event=action_begin tap_id=%s action=single_click", tap_id)
                    single_click_action(source="MPR121")
                    logger.info("MPR121 event=action_complete tap_id=%s action=single_click", tap_id)
                except Exception:
                    logger.exception("MPR121 event=action_failed tap_id=%s action=single_click", tap_id)
        finally:
            while True:
                try:
                    tap_id = self._pending.get_nowait()
                except queue.Empty:
                    break
                logger.info("MPR121 event=action_discarded tap_id=%s reason=stopping", tap_id)
            logger.info("MPR121 event=worker_stopped worker=action")

    def stop(self):
        logger.info("MPR121 event=stop_requested")
        self._stop.set()
        for thread in (self._poll_thread, self._action_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    logger.warning("MPR121 worker %s still stopping", thread.name)
        # The polling thread owns the bus after start, so never close its fd
        # underneath an in-flight ioctl. Its finally block closes it on exit.
