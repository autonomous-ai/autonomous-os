import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from core.utils.common import get_or_default

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")
PREDICTOR_T = TypeVar("PREDICTOR_T")


class PredictorFactory(Generic[PREDICTOR_T], ABC):
    """Base class for predictor factories.

    Subclasses store config (model path, thresholds, etc.) in __init__
    and create fresh predictor instances via create().
    """

    @abstractmethod
    def create(self) -> PREDICTOR_T:
        """Create and return a new (unstarted) predictor instance."""


class PredictorBase(Generic[INPUT_T, OUTPUT_T], ABC):
    DEFAULT_BATCH_SIZE: int = 1
    # Shared lock for PyTorch GPU inference. PyTorch models running concurrently
    # on the default CUDA stream cause cuDNN/stream collisions. Subclasses that
    # use PyTorch (ObjectDetector, PersonDetector) acquire this in predict().
    # ONNX Runtime predictors don't need it — they use a separate CUDA context.
    _gpu_lock: threading.RLock = threading.RLock()

    def __init__(self, batch_size: int | None = None) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        self._logger.setLevel(logging.DEBUG)
        self._lock: threading.RLock = threading.RLock()
        self._batch_size: int = get_or_default(batch_size, self.DEFAULT_BATCH_SIZE)

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
    def preprocess(self, input: list[INPUT_T]) -> list[Any]:
        """Preprocess a batch of inputs for inference."""

    @abstractmethod
    def _predict_impl(
        self, input: list[INPUT_T], *, preprocess: bool = True, **kwargs: Any
    ) -> list[OUTPUT_T]:
        """Internal prediction logic. Subclasses implement this instead of predict."""

    def start(self) -> None:
        with self._lock:
            self._start_impl()

    def stop(self) -> None:
        with self._lock:
            self._stop_impl()

    def is_ready(self) -> bool:
        with self._lock:
            return self._is_ready_impl()

    def predict(
        self, input: list[INPUT_T], *, preprocess: bool = True, **kwargs: Any
    ) -> list[OUTPUT_T]:
        """Make prediction on a batch of input. Thread-safe via lock.

        Large batches are chunked by ``_batch_size`` to limit peak memory.

        Args:
            input: Batch of inputs.
            preprocess: If True (default), run preprocess on each input
                before inference. Set to False when input is already
                preprocessed (e.g. from a buffer).

        Raises:
            RuntimeError: If the predictor is not ready.
        """
        with self._lock:
            if not self._is_ready_impl():
                raise RuntimeError(f"{self.__class__.__name__} is not ready")
            if len(input) <= self._batch_size:
                return self._predict_impl(input, preprocess=preprocess, **kwargs)
            results: list[OUTPUT_T] = []
            for i in range(0, len(input), self._batch_size):
                chunk = input[i : i + self._batch_size]
                results.extend(self._predict_impl(chunk, preprocess=preprocess, **kwargs))
            return results
