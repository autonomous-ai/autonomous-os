import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from core.perception.base.predictor import PredictorBase
from core.utils.common import deep_hash, get_or_default

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")


@dataclass
class BatchingQueueItem(Generic[INPUT_T, OUTPUT_T]):
    input: INPUT_T
    future: asyncio.Future[OUTPUT_T]
    kwargs_key: int = 0
    kwargs: dict[str, Any] = field(default_factory=dict)


class InputBatcher(Generic[INPUT_T, OUTPUT_T]):
    """Async batching layer that sits between sessions and a predictor.

    Multiple concurrent sessions submit individual inputs via ``submit()``.
    The batcher accumulates them and dispatches to the underlying predictor
    in batches, improving GPU utilization under concurrent load.

    Items with different kwargs (e.g. different ``classes`` for object
    detection) are grouped into separate sub-batches automatically.
    """

    DEFAULT_BATCH_SIZE: int = 1
    DEFAULT_BATCH_TIMEOUT_S: float = 0.1

    def __init__(
        self,
        predictor: PredictorBase[INPUT_T, OUTPUT_T],
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ):
        self._predictor: PredictorBase[INPUT_T, OUTPUT_T] = predictor
        self._batch_size: int = get_or_default(batch_size, self.DEFAULT_BATCH_SIZE)
        self._batch_timeout: float = get_or_default(batch_timeout, self.DEFAULT_BATCH_TIMEOUT_S)

        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        self._logger.setLevel(logging.DEBUG)

        self._running_loop: asyncio.Task[None] | None = None
        self._running: bool = False
        self._queue: asyncio.Queue[BatchingQueueItem[INPUT_T, OUTPUT_T]] | None = None
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._pending_items: deque[BatchingQueueItem[INPUT_T, OUTPUT_T]] = deque()
        self._last_dispatch_ts: float = 0

    @property
    def predictor(self) -> PredictorBase[INPUT_T, OUTPUT_T]:
        """Access the underlying predictor for non-inference methods.

        Use this for helpers like ``preprocess_single_frame()``,
        ``extract_crops()``, ``class_names``, etc.
        """
        return self._predictor

    async def start(self) -> None:
        self._last_dispatch_ts = 0
        self._pending_items = deque()
        self._running = True
        self._queue = None
        self._running_loop = None

        self._logger.info(
            "Ready (batch_size=%d, batch_timeout=%.3fs)",
            self._batch_size,
            self._batch_timeout,
        )

    def _ensure_loop(self) -> asyncio.Queue[BatchingQueueItem[INPUT_T, OUTPUT_T]]:
        """Create the queue and background loop in the current event loop.

        Returns the queue so callers can use it without None-checks.
        """
        current_loop = asyncio.get_running_loop()
        if self._bound_loop is not current_loop:
            self._bound_loop = current_loop
            self._queue = None
            self._running_loop = None
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._running_loop is None or self._running_loop.done():
            self._running_loop = asyncio.create_task(self._loop())
        return self._queue

    async def stop(self) -> None:
        self._running = False

        if self._running_loop is not None:
            self._running_loop.cancel()
            try:
                await self._running_loop
            except asyncio.CancelledError:
                pass
            self._running_loop = None

        # Reject any remaining pending items.
        for item in self._pending_items:
            if not item.future.done():
                item.future.set_exception(
                    RuntimeError("InputBatcher stopped before processing this item")
                )
        self._pending_items.clear()

        self._logger.info("Stopped")

    def is_ready(self) -> bool:
        return self._running and self._predictor.is_ready()

    async def submit(self, inputs: list[INPUT_T], **kwargs: Any) -> list[asyncio.Future[OUTPUT_T]]:
        """Submit inputs for batched inference.

        Returns a list of futures, one per input. Await them to get results.
        Extra kwargs are forwarded to ``predictor.predict()`` and are used
        to group items into sub-batches (items with the same kwargs get
        batched together).
        """
        queue = self._ensure_loop()
        kwargs_key = deep_hash(kwargs) if kwargs else 0
        futures: list[asyncio.Future[OUTPUT_T]] = []
        loop = asyncio.get_running_loop()
        for inp in inputs:
            future: asyncio.Future[OUTPUT_T] = loop.create_future()
            queue.put_nowait(
                BatchingQueueItem(
                    input=inp,
                    future=future,
                    kwargs_key=kwargs_key,
                    kwargs=kwargs,
                )
            )
            futures.append(future)

        return futures

    async def _loop(self) -> None:
        queue = self._queue
        if queue is None:
            raise RuntimeError("InputBatcher._loop started without a queue")
        while True:
            # Calculate how long to wait for the next item.
            wait_ts = max(
                self._batch_timeout - (time.monotonic() - self._last_dispatch_ts),
                0.001,  # avoid zero timeout (busy loop)
            )

            # Wait for a new item or timeout.
            try:
                new_item = await asyncio.wait_for(queue.get(), timeout=wait_ts)
                self._pending_items.append(new_item)
            except (asyncio.TimeoutError, TimeoutError):
                pass

            # Drain any remaining items from the queue.
            while True:
                try:
                    new_item = queue.get_nowait()
                    self._pending_items.append(new_item)
                except asyncio.QueueEmpty:
                    break

            if not self._pending_items:
                continue

            # Decide whether to dispatch.
            now_ts = time.monotonic()
            interval_ts = now_ts - self._last_dispatch_ts
            if len(self._pending_items) < self._batch_size and interval_ts <= self._batch_timeout:
                continue

            while self._pending_items:
                self._last_dispatch_ts = time.monotonic()
                # Collect up to batch_size items from pending.
                dispatch_items: list[BatchingQueueItem[INPUT_T, OUTPUT_T]] = []
                while len(dispatch_items) < self._batch_size and self._pending_items:
                    dispatch_items.append(self._pending_items.popleft())

                # Group by kwargs for correct dispatch.
                groups: dict[
                    int,
                    list[BatchingQueueItem[INPUT_T, OUTPUT_T]],
                ] = defaultdict(list)
                for item in dispatch_items:
                    groups[item.kwargs_key].append(item)

                # Dispatch each group.
                for items in groups.values():
                    input_batch: list[INPUT_T] = [it.input for it in items]
                    kwargs = items[0].kwargs
                    try:
                        output_batch: list[OUTPUT_T] = await asyncio.to_thread(
                            self._predictor.predict, input_batch, **kwargs
                        )
                        for i, out in enumerate(output_batch):
                            if not items[i].future.done():
                                items[i].future.set_result(out)
                    except Exception as exc:
                        for it in items:
                            if not it.future.done():
                                it.future.set_exception(exc)
