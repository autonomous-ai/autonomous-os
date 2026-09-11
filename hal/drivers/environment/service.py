"""Optional background environmental acquisition; no OS/agent dispatch."""

from dataclasses import asdict
import logging
import threading
import time

from hal.drivers.environment.sen55 import SEN55
from hal.board.sen55 import SEN55Timing

logger = logging.getLogger(__name__)


class EnvironmentService:
    def __init__(self, enabled=False, bus=None, driver_factory=SEN55, timing=None, name="sen55"):
        self.name = name
        self.timing = timing if timing is not None else SEN55Timing()
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
        self._first_sample_time = None

    def _set_state(self, state, error=None):
        with self._lock:
            self._state, self._error = state, error
            if state != "ready":
                self._first_sample_time = None

    def _store_sample(self, sample, sampled_at):
        with self._lock:
            if (self._first_sample_time is None or self._sample_time is None
                    or sampled_at - self._sample_time > self.timing.stale_after_s):
                self._first_sample_time = sampled_at
            self._sample = sample
            self._sample_time = sampled_at
            self._state, self._error = "ready", None

    def start(self):
        if self._thread is not None:
            return
        if not self.enabled:
            logger.info("[%s] disabled", self.name)
            return
        if self.bus is None or not str(self.bus).isascii() or not str(self.bus).isdecimal():
            self._set_state("error", f"{self.name} bus must be a nonnegative bus number")
            logger.error("[%s] cannot start: invalid I2C bus %r", self.name, self.bus)
            return
        self.bus = int(self.bus)
        self._set_state("starting")
        logger.info("[%s] starting: bus=%s poll_interval_s=%s", self.name, self.bus, self.timing.poll_interval_s)
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            driver = None
            try:
                driver = self._factory(int(self.bus))
                driver.start()
                logger.info("[%s] measurement started: bus=%s", self.name, self.bus)
                self._set_state("starting")
                last_data = time.monotonic()
                received = False
                while not self._stop.wait(self.timing.poll_interval_s):
                    sample = driver.read()
                    if sample is None:
                        logger.info("[%s] waiting for data: bus=%s last_data_age_s=%.3f", self.name, self.bus, time.monotonic() - last_data)
                    if sample is None and time.monotonic() - last_data > self.timing.no_data_timeout_s:
                        raise OSError(f"{self.name}: no new data for {self.timing.no_data_timeout_s:g} seconds")
                    if sample is not None:
                        last_data = time.monotonic()
                        self._store_sample(sample, last_data)
                        if not received:
                            logger.info("[%s] receiving data: fields=%s", self.name, ",".join(sample))
                            received = True
                        logger.info("[%s] sample=%s", self.name, sample)
            except Exception as exc:
                self._set_state("error", str(exc))
                logger.warning("[%s] acquisition failed: %s; retry_interval_s=%s", self.name, exc, self.timing.retry_interval_s)
            finally:
                if driver is not None:
                    try:
                        driver.close()
                    except Exception as exc:
                        logger.warning("[%s] close failed: %s", self.name, exc)
            if self._stop.wait(self.timing.retry_interval_s):
                break
        self._set_state("stopped")
        logger.info("[%s] stopped", self.name)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                self._set_state("error", f"{self.name} worker did not stop within 3 seconds")
                logger.error("[%s] worker did not stop within 3 seconds", self.name)
                return
        if self.enabled:
            self._set_state("stopped")

    def snapshot(self):
        with self._lock:
            age = None if self._sample_time is None else time.monotonic() - self._sample_time
            stale = age is None or age > self.timing.stale_after_s or self._state != "ready"
            # Count successful acquisition time, never time spent waiting for data.
            continuous_data = None
            if self.enabled and not stale and self._first_sample_time is not None:
                continuous_data = self._sample_time - self._first_sample_time
            return {
                "state": self._state, "enabled": self.enabled, "bus": self.bus,
                "last_error": self._error,
                "timing": asdict(self.timing),
                "sample": None if self._sample is None else dict(self._sample),
                "age_s": age,
                "stale": stale,
                "continuous_data_s": continuous_data,
            }
