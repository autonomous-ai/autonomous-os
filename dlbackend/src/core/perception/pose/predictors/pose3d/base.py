"""Abstract base class for 3D pose lifters.

Extends PredictorBase. Input is a temporal sequence of 2D keypoints,
output is a batched 3D joint prediction. Subclasses override class-level
defaults (model path, n_frames, input_size).

The input type for PredictorBase is a tuple of (keypoints, scores) arrays
representing one temporal sequence. The output is RawPose3DDetection.
"""

from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import RawPose3DDetection
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session

# Input type: (keypoints (T, K, 2), scores (T, K))
Pose3DInput = tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]


class PoseEstimator3DLifting(PredictorBase[Pose3DInput, RawPose3DDetection | None]):
    """Base class for 3D pose lifters (e.g. TCPFormer).

    Takes temporal sequences of 2D keypoints, normalizes, pads/truncates
    to n_frames, and runs ONNX inference.
    """

    GRAPH_TYPE: GraphEnum

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_N_FRAMES: int = 243
    DEFAULT_INPUT_SIZE: tuple[int, int] = (1920, 1080)
    ONNX_INPUT_NAME: str = "keypoints"
    ONNX_OUTPUT_NAME: str = "poses"
    ONNX_NUM_JOINTS: int = 17

    # H36M joint index for neck (used as center for normalization)
    NECK_JOINT_IDX: int = 9

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        input_size: tuple[int, int] | None = None,
        n_frames: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._input_size: tuple[int, int] = get_or_default(input_size, self.DEFAULT_INPUT_SIZE)
        self._n_frames: int = get_or_default(n_frames, self.DEFAULT_N_FRAMES)

        self._session: ort.InferenceSession | None = None
        self._running: bool = False

    @property
    def n_frames(self) -> int:
        return self._n_frames

    @property
    def input_size(self) -> tuple[int, int]:
        return self._input_size

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading model from %s", self._model_path)
        warmup = {self.ONNX_INPUT_NAME: np.zeros(
            (self._batch_size, self._n_frames, self.ONNX_NUM_JOINTS, 3), dtype=np.float32,
        )}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @override
    def preprocess(self, input: list[Pose3DInput]) -> list[npt.NDArray[np.float32]]:
        """Normalize, center on neck, pad/truncate to n_frames, append confidence.

        Each input is (keypoints (T, K, 2), scores (T, K)).
        Returns list of (n_frames, K, 3) tensors.
        """
        W, H = self._input_size
        results: list[npt.NDArray[np.float32]] = []
        for keypoints, scores in input:
            norm_kps: npt.NDArray[np.float32] = keypoints.copy()
            # Center around neck joint
            neck: npt.NDArray[np.float32] = norm_kps[
                :, self.NECK_JOINT_IDX : self.NECK_JOINT_IDX + 1, :
            ]
            norm_kps = norm_kps - neck
            # Normalize to [-1, 1]
            norm_kps[..., 0] = norm_kps[..., 0] / W * 2
            norm_kps[..., 1] = norm_kps[..., 1] / H * 2
            # Append confidence as 3rd channel
            norm: npt.NDArray[np.float32] = np.concatenate(
                [norm_kps, scores[..., None]], axis=-1
            ).astype(np.float32)  # (T, K, 3)
            # Pad or truncate to the fixed temporal window the model expects.
            # Short clips are padded by REPEATING the last frame (not zero-padding):
            # the lifter is trained on continuous motion, so a held final pose is a
            # benign "person stopped moving" signal, whereas zeros would inject an
            # impossible jump to the origin and corrupt the temporal features.
            # Long clips keep the MOST RECENT n_frames (tail) so the prediction
            # tracks the current pose rather than stale history.
            T: int = norm.shape[0]
            if T < self._n_frames:
                norm = np.concatenate(
                    [norm, np.repeat(norm[-1:], repeats=self._n_frames - T, axis=0)],
                    axis=0,
                )
            else:
                norm = norm[-self._n_frames :]
            results.append(norm)
        return results

    @override
    def _predict_impl(
        self,
        input: list[Pose3DInput],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawPose3DDetection | None]:
        """Lift 2D keypoint sequences to 3D.

        Each input is (keypoints (T, K, 2), scores (T, K)).
        Returns None for sequences with fewer than n_frames // 2 frames.
        """
        results: list[RawPose3DDetection | None] = []
        preprocessed: list[npt.NDArray[np.float32]] = []

        # Check minimum frame count and preprocess
        for keypoints, scores in input:
            if preprocess:
                preprocessed.append(self.preprocess([(keypoints, scores)])[0])
            else:
                preprocessed.append(keypoints)  # type: ignore

        # Stack valid inputs: (B, n_frames, K, 3)
        batch: npt.NDArray[np.float32] = np.stack(preprocessed, axis=0).astype(np.float32)

        with self._gpu_lock:
            (output,) = self._session.run([self.ONNX_OUTPUT_NAME], {self.ONNX_INPUT_NAME: batch})
        output = cast(npt.NDArray[np.float32], output)  # (B, n_frames, K, 3)

        # Map results back — trim padded frames to original T
        result_map: dict[int, npt.NDArray[np.float32]] = {}
        for idx in range(output.shape[0]):
            original_T: int = input[idx][0].shape[0]
            result_map[idx] = output[idx, :original_T]  # (T, K, 3)

        for i in range(len(input)):
            if i in result_map:
                results.append(RawPose3DDetection(joints_3d=result_map[i]))  # (T, K, 3)
            else:
                results.append(None)

        return results
