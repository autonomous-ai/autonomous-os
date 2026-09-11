"""Optional background environmental acquisition; no OS/agent dispatch."""

import logging
import threading
import time

from hal.drivers.environment.sen55 import SEN55

logger = logging.getLogger(__name__)


class EnvironmentService:
    def __init__(self, enabled=False, bus=None, driver_factory=SEN55):
        self.enabled = enabled
        self.bus = bus
        self._factory = driver_factory
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._state = "disabled"
        self._error = None
        self._sample = None
        self._sample_time = None

    def _set_state(self, state, error=None):
        with self._lock:
            self._state, self._error = state, error

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        if self.bus is None or not str(self.bus).isascii() or not str(self.bus).isdecimal():
            self._set_state("error", "SEN55 bus must be a nonnegative bus number")
            return
        self.bus = int(self.bus)
        self._set_state("starting")
        self._thread = threading.Thread(target=self._run, name="sen55", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            driver = None
            try:
                driver = self._factory(int(self.bus))
                driver.start()
                self._set_state("starting")
                last_data = time.monotonic()
                while not self._stop.wait(1.0):
                    sample = driver.read()
                    if sample is None and time.monotonic() - last_data > 30.0:
                        raise OSError("SEN55: no new data for 30 seconds")
                    if sample is not None:
                        last_data = time.monotonic()
                        with self._lock:
                            self._sample = sample
                            self._sample_time = time.monotonic()
                            self._state, self._error = "ready", None
            except Exception as exc:
                self._set_state("error", str(exc))
                logger.warning("SEN55 acquisition failed: %s", exc)
            finally:
                if driver is not None:
                    try:
                        driver.close()
                    except Exception as exc:
                        logger.warning("SEN55 close failed: %s", exc)
            if self._stop.wait(5.0):
                break
        self._set_state("stopped")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._set_state("error", "SEN55 worker did not stop within 3 seconds")
                return
        if self.enabled:
            self._set_state("stopped")

    def snapshot(self):
        with self._lock:
            age = None if self._sample_time is None else time.monotonic() - self._sample_time
            return {
                "state": self._state, "enabled": self.enabled, "bus": self.bus,
                "last_error": self._error,
                "sample": None if self._sample is None else dict(self._sample),
                "age_s": age,
                "stale": age is None or age > 5.0 or self._state != "ready",
            }
