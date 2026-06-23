"""Per-connection pose estimation session."""

import asyncio
import time
from typing import Any

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import (
    Point2D,
    Point3D,
    Pose2D,
    Pose3D,
    PoseDetection,
    PosePerceptionSessionConfig,
)
from core.perception.base import PerceptionSessionBase
from core.perception.pose.graph.convert import convert_graph
from core.perception.pose.predictors.ergo.base import ErgoAssessor, ErgoInput
from core.perception.pose.predictors.pose2d.base import PoseEstimator2D
from core.perception.pose.predictors.pose3d.base import PoseEstimator3DLifting
from core.types import Omit, omit
from core.utils.common import get_or_default


class PosePerceptionSession(
    PerceptionSessionBase[
        cv2t.MatLike,
        PoseDetection,
        PosePerceptionSessionConfig,
    ]
):
    DEFAULT_CONFIG: PosePerceptionSessionConfig = PosePerceptionSessionConfig()

    def __init__(
        self,
        estimator_2d: PoseEstimator2D,
        lifter_3d: PoseEstimator3DLifting | None = None,
        ergo_assessor: ErgoAssessor | None = None,
        config: PosePerceptionSessionConfig | None = None,
    ) -> None:
        config = get_or_default(config, PosePerceptionSessionConfig())
        super().__init__(config)

        self._estimator_2d: PoseEstimator2D = estimator_2d
        self._lifter_3d: PoseEstimator3DLifting | None = lifter_3d
        self._ergo_assessor: ErgoAssessor | None = ergo_assessor

        self._kps_buffer: list[npt.NDArray[np.float32]] = []
        self._scores_buffer: list[npt.NDArray[np.float32]] = []

        self._running: bool = False

    @override
    async def start(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._running = True

    @override
    async def stop(self) -> None:
        self._running = False

    @override
    def is_ready(self) -> bool:
        if not self._estimator_2d.is_ready():
            return False
        if self._lifter_3d is not None and not self._lifter_3d.is_ready():
            return False
        return self._running

    @override
    async def update(self, input: cv2t.MatLike) -> PoseDetection | None:
        """Run pose estimation on a single frame.

        Returns PoseDetection with 2D (always), optional 3D and ergo,
        or None if rate-limited.
        """
        cur_ts: float = time.time()
        if cur_ts - self._last_update_ts < self._config.frame_interval:
            return self._last_prediction

        # 2D estimation (batch of 1)
        raw_2d = (await asyncio.to_thread(self._estimator_2d.predict, [input]))[0]
        kps: npt.NDArray[np.float32] = raw_2d.keypoints[0]  # (K, 2)
        confs: npt.NDArray[np.float32] = raw_2d.scores[0]  # (K,)
        src_graph: GraphEnum = self._estimator_2d.GRAPH_TYPE

        pose_2d: Pose2D = Pose2D(
            graph_type=src_graph,
            joints=[Point2D(x=float(kps[i, 0]), y=float(kps[i, 1])) for i in range(len(kps))],
            confs=[float(c) for c in confs],
        )

        result: PoseDetection = PoseDetection(pose_2d=pose_2d)

        # Check if enough keypoints are confident for downstream
        num_valid: int = int((confs >= self._config.confidence_threshold_2d).sum())
        has_valid_pose: bool = num_valid >= self._config.min_valid_keypoints

        # Optional 3D lifting (2D graph → lifter graph)
        lifter_kps: npt.NDArray[np.float32] | None = None
        lifter_scores: npt.NDArray[np.float32] | None = None
        if self._lifter_3d is not None and has_valid_pose:
            lifter_kps, lifter_scores = convert_graph(
                raw_2d.keypoints,
                raw_2d.scores,
                src_graph,
                self._lifter_3d.GRAPH_TYPE,
            )

            self._kps_buffer.append(lifter_kps[0])
            self._scores_buffer.append(lifter_scores[0])

            n_frames: int = self._lifter_3d.n_frames
            if len(self._kps_buffer) > n_frames:
                self._kps_buffer = self._kps_buffer[-n_frames:]
            if len(self._scores_buffer) > n_frames:
                self._scores_buffer = self._scores_buffer[-n_frames:]

            kps_stack: npt.NDArray[np.float32] = np.stack(self._kps_buffer, axis=0)
            scores_stack: npt.NDArray[np.float32] = np.stack(self._scores_buffer, axis=0)

            raw_3d = (await asyncio.to_thread(
                self._lifter_3d.predict, [(kps_stack, scores_stack)],
            ))[0]
            if raw_3d is not None:
                joints_last: npt.NDArray[np.float32] = raw_3d.joints_3d[-1]  # (K, 3)
                result.pose_3d = Pose3D(
                    graph_type=self._lifter_3d.GRAPH_TYPE,
                    joints=[
                        Point3D(
                            x=float(joints_last[i, 0]),
                            y=float(joints_last[i, 1]),
                            z=float(joints_last[i, 2]),
                        )
                        for i in range(len(joints_last))
                    ],
                    confs=[float(c) for c in lifter_scores[0]],
                )

        # Optional ergo assessment
        if self._ergo_assessor is not None and has_valid_pose:
            ergo_graph: GraphEnum = self._ergo_assessor.GRAPH_TYPE

            if (
                result.pose_3d is not None
                and self._lifter_3d is not None
                and lifter_kps is not None
                and lifter_scores is not None
            ):
                # Prefer 3D output → convert lifter graph → ergo graph
                ergo_kps, ergo_scores = convert_graph(
                    lifter_kps,
                    lifter_scores,  # type: ignore[arg-type]
                    self._lifter_3d.GRAPH_TYPE,
                    ergo_graph,
                )
            else:
                # No 3D lifter — convert 2D graph → ergo graph
                ergo_kps, ergo_scores = convert_graph(
                    raw_2d.keypoints,
                    raw_2d.scores,
                    src_graph,
                    ergo_graph,
                )

            ergo_input: ErgoInput = (ergo_kps[0], ergo_scores[0])
            ergo_result = (await asyncio.to_thread(
                self._ergo_assessor.predict, [ergo_input],
            ))[0]
            if ergo_result is not None:
                result.ergo = ergo_result

        self._last_update_ts = cur_ts
        self._last_prediction = result

        if result.pose_2d.joints:
            self._logger.info(
                "[session %s] Pose: %d joints, 3D=%s, ergo=%d",
                self._session_id,
                len(result.pose_2d.joints),
                result.pose_3d is not None,
                result.ergo.score if result.ergo is not None else -1,
            )

        return result

    @override
    def update_config(
        self,
        *,
        frame_interval: float | Omit = omit,
        confidence_threshold_2d: float | Omit = omit,
        min_valid_keypoints: int | Omit = omit,
        **kwargs: Any,
    ) -> None:
        super().update_config(
            frame_interval=frame_interval,
            confidence_threshold_2d=confidence_threshold_2d,
            min_valid_keypoints=min_valid_keypoints,
        )

    @override
    def _post_config_update(self) -> None:
        self._logger.info(
            "[session %s] Config updated — frame_interval=%.2f, confidence_threshold_2d=%.2f, min_valid_keypoints=%d",
            self._session_id,
            self._config.frame_interval,
            self._config.confidence_threshold_2d,
            self._config.min_valid_keypoints,
        )
