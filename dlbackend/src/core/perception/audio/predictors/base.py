"""Audio embedder base class using WeSpeaker ONNX models with sliding window."""

from pathlib import Path
from typing import Any

import kaldi_native_fbank as knf
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.audio import RawAudioEmbedding
from core.models.media import Audio
from core.perception.audio.processors import CompositeAudioProcessor
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class AudioEmbedder(PredictorBase[Audio, RawAudioEmbedding]):
    """Base audio embedder using WeSpeaker ONNX models.

    Computes 80-dim fbank features, applies sliding windows with 50% overlap,
    runs ONNX inference per window, and mean-aggregates with L2 normalization.
    For audio shorter than one window, features are linearly interpolated to
    the window size.
    """

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_PROCESSOR_FACTORY: AudioProcessorFactory = AudioProcessorFactory()

    DEFAULT_WINDOW_FRAMES: int = 200
    DEFAULT_HOP_FRAMES: int = 100
    DEFAULT_SAMPLE_RATE: int = 16000
    DEFAULT_NUM_MEL_BINS: int = 80
    ONNX_INPUT_NAME: str = "feats"

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        processor_factory: AudioProcessorFactory | None = None,
        window_frames: int | None = None,
        hop_frames: int | None = None,
        sample_rate: int | None = None,
        num_mel_bins: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        self._model_path: Path | None = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._processor_factory: AudioProcessorFactory = get_or_default(
            processor_factory, self.DEFAULT_PROCESSOR_FACTORY
        )
        self._window_frames: int = get_or_default(window_frames, self.DEFAULT_WINDOW_FRAMES)
        self._hop_frames: int = get_or_default(hop_frames, self.DEFAULT_HOP_FRAMES)
        self._sample_rate: int = get_or_default(sample_rate, self.DEFAULT_SAMPLE_RATE)
        self._num_mel_bins: int = get_or_default(num_mel_bins, self.DEFAULT_NUM_MEL_BINS)

        self._session: ort.InferenceSession | None = None
        self._processor: CompositeAudioProcessor | None = None

    @override
    def _start_impl(self) -> None:
        if self._session is not None:
            self._logger.info("Already running")
            return

        if self._model_path is None:
            raise RuntimeError(f"{self.__class__.__name__} has no model_path configured")

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._processor = self._processor_factory.create()
        self._processor.start()
        self._logger.info("Loading audio embedder from %s", self._model_path)
        warmup = {self.ONNX_INPUT_NAME: np.zeros(
            (self._batch_size, self._window_frames, self._num_mel_bins), dtype=np.float32,
        )}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._logger.info("Audio embedder started")

    @override
    def _stop_impl(self) -> None:
        self._session = None
        if self._processor is not None:
            self._processor.stop()
            self._processor = None
        self._logger.info("Audio embedder stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._session is not None and self._processor is not None

    @override
    def preprocess(self, input: list[Audio]) -> list[Audio]:
        """Run the composite audio processor on each input."""
        return [self._processor.process(audio) for audio in input]

    def _compute_fbank(self, audio: Audio) -> npt.NDArray[np.float32]:
        """Compute fbank features using kaldi-native-fbank.

        Returns:
            Feature array of shape (T, num_mel_bins) after CMN.
        """
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = float(self._sample_rate)
        opts.frame_opts.frame_length_ms = 25.0
        opts.frame_opts.frame_shift_ms = 10.0
        opts.frame_opts.dither = 0.0
        opts.frame_opts.window_type = "hamming"
        opts.mel_opts.num_bins = self._num_mel_bins

        fbank = knf.OnlineFbank(opts)
        fbank.accept_waveform(self._sample_rate, audio.waveform)
        fbank.input_finished()

        num_frames = fbank.num_frames_ready
        if num_frames == 0:
            return np.zeros((0, self._num_mel_bins), dtype=np.float32)

        feat = np.stack(
            [np.array(fbank.get_frame(i), dtype=np.float32) for i in range(num_frames)]
        )  # (T, num_mel_bins)

        # Cepstral mean normalization
        feat = feat - feat.mean(axis=0)
        return feat

    def _sliding_windows(
        self, feat: npt.NDArray[np.float32]
    ) -> npt.NDArray[np.float32]:
        """Split fbank features into overlapping windows.

        If T < window_frames, linearly interpolate to window_frames.
        Otherwise, slide with hop_frames overlap. The last window is shifted
        back so that all windows have exactly window_frames size.

        Args:
            feat: Shape (T, num_mel_bins).

        Returns:
            Array of shape (N, window_frames, num_mel_bins).
        """
        T = feat.shape[0]

        if T == 0:
            return np.zeros((1, self._window_frames, self._num_mel_bins), dtype=np.float32)

        if T < self._window_frames:
            # Clip shorter than one window: time-STRETCH it to a full window via
            # per-mel-bin linear interpolation, rather than zero-padding. Fbank
            # features vary smoothly over time, so stretching preserves the spectral
            # shape the speaker embedder relies on, whereas appended silence (zeros)
            # would dilute the embedding and pull short utterances toward a common
            # "mostly-silence" point. Interpolation is along time only; each mel bin
            # is interpolated independently (the `for m` loop).
            x_old = np.linspace(0, 1, T)
            x_new = np.linspace(0, 1, self._window_frames)
            interpolated = np.stack(
                [np.interp(x_new, x_old, feat[:, m]) for m in range(feat.shape[1])],
                axis=1,
            ).astype(np.float32)
            return interpolated[np.newaxis]  # (1, W, M)

        windows: list[npt.NDArray[np.float32]] = []
        start = 0
        while start + self._window_frames <= T:
            windows.append(feat[start : start + self._window_frames])
            start += self._hop_frames

        if start < T:
            windows.append(feat[T - self._window_frames : T])

        return np.stack(windows)  # (N, W, M)

    def _infer_batch(
        self, windows: npt.NDArray[np.float32]
    ) -> npt.NDArray[np.float32]:
        """Run ONNX inference on windows in mini-batches.

        Args:
            windows: Shape (N, window_frames, num_mel_bins).

        Returns:
            L2-normalized embeddings of shape (N, D).
        """
        parts: list[npt.NDArray[np.float32]] = []

        for i in range(0, len(windows), self._batch_size):
            batch = windows[i : i + self._batch_size]  # (B, W, M)
            with self._gpu_lock:
                (output,) = self._session.run(None, {self.ONNX_INPUT_NAME: batch})
            output = np.asarray(output, dtype=np.float32)  # (B, D)

            norms = np.linalg.norm(output, axis=1, keepdims=True)
            norms = np.maximum(norms, 1e-10)
            output = output / norms

            parts.append(output)

        return np.concatenate(parts, axis=0)  # (N, D)

    def _mean_aggregate(
        self, chunk_embeddings: npt.NDArray[np.float32]
    ) -> npt.NDArray[np.float32]:
        """Mean-aggregate chunk embeddings with L2 normalization.

        Args:
            chunk_embeddings: L2-normalized embeddings of shape (N, D).

        Returns:
            L2-normalized mean embedding of shape (D,).
        """
        mean = chunk_embeddings.mean(axis=0)
        norm = np.linalg.norm(mean)

        return (mean / (norm + 1e-10)).astype(np.float32)

    @override
    def _predict_impl(
        self,
        input: list[Audio],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawAudioEmbedding]:
        """Run audio embedding on a batch of audio inputs.

        Per audio: preprocess → fbank → sliding windows → ONNX → aggregate.
        """
        if preprocess:
            input = self.preprocess(input)

        results: list[RawAudioEmbedding] = []

        for audio in input:
            feat = self._compute_fbank(audio)
            windows = self._sliding_windows(feat)
            chunk_embeddings = self._infer_batch(windows)
            embedding = self._mean_aggregate(chunk_embeddings)

            results.append(
                RawAudioEmbedding(
                    embedding=embedding,
                    chunk_embeddings=chunk_embeddings,
                )
            )

        return results
