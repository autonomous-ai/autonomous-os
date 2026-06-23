"""Base audio emotion predictor — classifies emotion from audio waveforms.

Takes a batch of Audio inputs, preprocesses (mono + resample), runs ONNX
inference with zero-padding for variable-length batching, and returns raw
expression probability distributions.

Concrete subclasses (Emotion2Vec) override class-level defaults
(model path, labels file, sample rate).
"""

from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.audio_emotion import RawAudioEmotionDetection
from core.models.media import Audio
from core.perception.audio.processors import CompositeAudioProcessor
from core.perception.audio.processors.utils import AudioProcessorFactory
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class AudioEmotionRecognizer(PredictorBase[Audio, RawAudioEmotionDetection]):
    """Base class for audio emotion classifiers.

    Subclasses override class-level defaults. The base handles ONNX
    lifecycle, preprocessing, and inference.
    """

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_LABELS_PATH: Path | None = None
    DEFAULT_SAMPLE_RATE: int = 16000
    DEFAULT_PROCESSOR_FACTORY: AudioProcessorFactory = AudioProcessorFactory(
        target_sample_rate=16000,
        enable_resample=True,
        enable_high_pass=False,
        enable_noise_reduce=False,
        enable_vad=False,
        enable_rms_normalize=False,
    )
    ONNX_INPUT_NAME: str = "input"
    ONNX_OUTPUT_NAME: str = "logits"

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        labels_path: Path | None = None,
        processor_factory: AudioProcessorFactory | None = None,
        sample_rate: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        labels_path = get_or_default(labels_path, self.DEFAULT_LABELS_PATH)
        if labels_path is None:
            raise RuntimeError("labels_path must not be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._labels_path: Path = labels_path
        self._processor_factory: AudioProcessorFactory = get_or_default(
            processor_factory, self.DEFAULT_PROCESSOR_FACTORY
        )
        self._sample_rate: int = get_or_default(sample_rate, self.DEFAULT_SAMPLE_RATE)

        self._class_names: list[str] = []
        self._running: bool = False
        self._session: ort.InferenceSession | None = None
        self._processor: CompositeAudioProcessor | None = None

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._processor = self._processor_factory.create()
        self._processor.start()
        self._logger.info("Loading model from %s", self._model_path)
        warmup_t = self._sample_rate * 10  # 10s at target sample rate
        warmup = {self.ONNX_INPUT_NAME: np.zeros((self._batch_size, warmup_t), dtype=np.float32)}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._class_names = self._load_classes(self._labels_path)
        self._running = True
        self._logger.info("Ready — %d emotion classes", len(self._class_names))

    @override
    def _stop_impl(self) -> None:
        self._session = None
        if self._processor is not None:
            self._processor.stop()
            self._processor = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None and self._processor is not None

    @override
    def preprocess(self, input: list[Audio]) -> list[Audio]:
        """Run the composite audio processor on each input."""
        return [self._processor.process(audio) for audio in input]

    @override
    def _predict_impl(
        self,
        input: list[Audio],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawAudioEmotionDetection]:
        """Classify emotion for a batch of audio utterances.

        Zero-pads shorter waveforms to max length in batch, stacks into
        [N, T_max], and runs ONNX inference in one pass.
        """
        if preprocess:
            input = self.preprocess(input)

        waveforms = [audio.waveform for audio in input]
        max_t = max(w.shape[0] for w in waveforms)

        batch = np.zeros((len(waveforms), max_t), dtype=np.float32)
        for i, w in enumerate(waveforms):
            batch[i, : w.shape[0]] = w

        with self._gpu_lock:
            raw_outputs: list[npt.NDArray[np.float32]] = cast(
                list[npt.NDArray[np.float32]],
                self._session.run([self.ONNX_OUTPUT_NAME], {self.ONNX_INPUT_NAME: batch}),
            )
        return self._postprocess_batch(raw_outputs, len(input))

    def _postprocess_batch(
        self, raw_outputs: list[npt.NDArray[np.float32]], N: int
    ) -> list[RawAudioEmotionDetection]:
        """Convert batched ONNX output to per-sample RawAudioEmotionDetection.

        Softmax is baked into the ONNX graph — output is probs directly.
        """
        probs: npt.NDArray[np.float32] = np.asarray(raw_outputs[0], dtype=np.float32)

        return [RawAudioEmotionDetection(expression_probs=probs[i]) for i in range(N)]

    @staticmethod
    def _load_classes(classes_path: Path) -> list[str]:
        return classes_path.read_text().strip().split("\n")
