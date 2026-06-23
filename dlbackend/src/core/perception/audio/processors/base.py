"""Audio processor base class."""

from abc import ABC
from typing_extensions import override

from core.models.media import Audio
from core.perception.base.processor import InputProcessorBase


class AudioProcessorBase(InputProcessorBase[Audio, Audio], ABC):
    """Base for audio processors. Input and output are both Audio.

    Default lifecycle is no-op (ready immediately). Subclasses that load
    resources (e.g. VAD model) override _start_impl/_stop_impl/_is_ready_impl.
    """

    def __init__(self) -> None:
        super().__init__()
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        self._running = True
        self._logger.info("Processor started")

    @override
    def _stop_impl(self) -> None:
        self._running = False
        self._logger.info("Processor stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running
