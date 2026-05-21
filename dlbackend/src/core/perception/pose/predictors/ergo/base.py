"""Abstract base class for ergonomic assessment from pose keypoints.

Extends PredictorBase. Input is (keypoints, scores) for a single frame,
output is ErgoAssessment or None.
"""

from abc import ABC

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import ErgoAssessment
from core.perception.base import PredictorBase

# Input type: (keypoints (K, 2|3), scores (K,))
ErgoInput = tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]


class ErgoAssessor(PredictorBase[ErgoInput, ErgoAssessment | None], ABC):
    """Base interface for ergonomic assessors that operate on pose keypoints.

    Unlike ONNX-based predictors, ergo assessors are pure computation
    (no model to load). start/stop are no-ops by default.
    """

    GRAPH_TYPE: GraphEnum

    def __init__(self) -> None:
        super().__init__()
        self._running: bool = True

    @override
    def _start_impl(self) -> None:
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running

    @override
    def preprocess(self, input: list[ErgoInput]) -> list[ErgoInput]:
        """No preprocessing needed for ergo assessment."""
        return input
