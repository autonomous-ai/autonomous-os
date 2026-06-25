"""Pose estimation pipeline: model lifecycle and session management."""

import asyncio

from typing_extensions import override

from core.models.pose import PosePerceptionSessionConfig
from core.perception.base import PerceptionBase
from core.perception.base.batching import InputBatcher
from core.perception.pose.predictors.ergo.base import ErgoAssessor, ErgoAssessment, ErgoInput
from core.perception.pose.predictors.pose2d.base import PoseEstimator2D, RawPose2DDetection
from core.perception.pose.predictors.pose3d.base import (
    Pose3DInput,
    PoseEstimator3DLifting,
    RawPose3DDetection,
)
from core.perception.pose.session import PosePerceptionSession
from core.perception.pose.utils import (
    ErgoAssessorFactory,
    PoseEstimator2DFactory,
    PoseLifter3DFactory,
)

import cv2.typing as cv2t


class PosePerception(PerceptionBase[PosePerceptionSession]):
    """Pose estimation pipeline. Loaded once, shared by all WS sessions."""

    def __init__(
        self,
        estimator_2d_factory: PoseEstimator2DFactory,
        lifter_3d_factory: PoseLifter3DFactory | None = None,
        ergo_assessor_factory: ErgoAssessorFactory | None = None,
        default_config: PosePerceptionSessionConfig | None = None,
        batch_size: int | None = None,
        batch_timeout: float | None = None,
    ) -> None:
        super().__init__()

        self._estimator_2d_factory: PoseEstimator2DFactory = estimator_2d_factory
        self._lifter_3d_factory: PoseLifter3DFactory | None = lifter_3d_factory
        self._ergo_assessor_factory: ErgoAssessorFactory | None = ergo_assessor_factory
        self._default_config: PosePerceptionSessionConfig | None = default_config

        self._batch_size: int | None = batch_size
        self._batch_timeout: float | None = batch_timeout

        self._estimator_2d: PoseEstimator2D | None = None
        self._lifter_3d: PoseEstimator3DLifting | None = None
        self._ergo_assessor: ErgoAssessor | None = None

        self._pose2d_batcher: InputBatcher[cv2t.MatLike, RawPose2DDetection] | None = None
        self._pose3d_batcher: InputBatcher[Pose3DInput, RawPose3DDetection | None] | None = None
        self._ergo_batcher: InputBatcher[ErgoInput, ErgoAssessment | None] | None = None
        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._estimator_2d = self._estimator_2d_factory.create()
        await asyncio.to_thread(self._estimator_2d.start)
        self._pose2d_batcher = InputBatcher(self._estimator_2d, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
        await self._pose2d_batcher.start()

        if self._lifter_3d_factory is not None:
            self._lifter_3d = self._lifter_3d_factory.create()
            await asyncio.to_thread(self._lifter_3d.start)
            self._pose3d_batcher = InputBatcher(self._lifter_3d, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
            await self._pose3d_batcher.start()

        if self._ergo_assessor_factory is not None:
            self._ergo_assessor = self._ergo_assessor_factory.create()
            await asyncio.to_thread(self._ergo_assessor.start)
            self._ergo_batcher = InputBatcher(self._ergo_assessor, batch_size=self._batch_size, batch_timeout=self._batch_timeout)
            await self._ergo_batcher.start()

        self._running = True
        self._logger.info("Ready")

    @override
    async def stop(self) -> None:
        if self._pose2d_batcher is not None:
            await self._pose2d_batcher.stop()
            self._pose2d_batcher = None

        if self._pose3d_batcher is not None:
            await self._pose3d_batcher.stop()
            self._pose3d_batcher = None

        if self._ergo_batcher is not None:
            await self._ergo_batcher.stop()
            self._ergo_batcher = None

        if self._estimator_2d is not None:
            await asyncio.to_thread(self._estimator_2d.stop)
            self._estimator_2d = None

        if self._lifter_3d is not None:
            await asyncio.to_thread(self._lifter_3d.stop)
            self._lifter_3d = None

        if self._ergo_assessor is not None:
            await asyncio.to_thread(self._ergo_assessor.stop)
            self._ergo_assessor = None

        self._running = False
        self._logger.info("Stopped")

    @override
    def is_ready(self) -> bool:
        if not self._running or self._pose2d_batcher is None:
            return False
        if not self._pose2d_batcher.is_ready():
            return False
        if self._pose3d_batcher is not None and not self._pose3d_batcher.is_ready():
            return False
        return True

    @override
    async def create_session(self) -> PosePerceptionSession:
        if self._pose2d_batcher is None:
            raise RuntimeError("PosePerception not started")

        config = self._default_config or PosePerceptionSession.DEFAULT_CONFIG
        return PosePerceptionSession(
            pose2d_batcher=self._pose2d_batcher,
            pose3d_batcher=self._pose3d_batcher,
            ergo_batcher=self._ergo_batcher,
            config=config,
        )
