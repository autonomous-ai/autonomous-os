"""Abstract base class for input processors."""

import logging
import threading
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")


class InputProcessorBase(Generic[INPUT_T, OUTPUT_T], ABC):
    """Base for input processors with lifecycle management."""

    def __init__(self) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        self._logger.setLevel(logging.DEBUG)
        self._lock: threading.RLock = threading.RLock()

    @abstractmethod
    def _start_impl(self) -> None:
        pass

    @abstractmethod
    def _stop_impl(self) -> None:
        pass

    @abstractmethod
    def _is_ready_impl(self) -> bool:
        pass

    @abstractmethod
    def _process_impl(self, input: INPUT_T) -> OUTPUT_T:
        """Process a single input. Subclasses implement this."""

    def start(self) -> None:
        with self._lock:
            self._start_impl()

    def stop(self) -> None:
        with self._lock:
            self._stop_impl()

    def is_ready(self) -> bool:
        with self._lock:
            return self._is_ready_impl()

    def process(self, input: INPUT_T) -> OUTPUT_T:
        """Process a single input. Thread-safe via lock."""
        with self._lock:
            if not self._is_ready_impl():
                raise RuntimeError(f"{self.__class__.__name__} is not ready")

        return self._process_impl(input)
