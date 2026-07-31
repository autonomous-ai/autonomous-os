"""Speech-intelligibility gate (SQUIM-STOI) — rejects noisy / broken-voice audio.

A reference-free STOI estimator (TorchAudio ``SQUIM_OBJECTIVE`` STOI branch,
exported to ONNX) run as a quality gate on the speaker-recognition preprocessing
pipeline. Higher STOI ≈ more intelligible speech; a clip whose **mean chunk
score** falls below the threshold is rejected (``PreprocessRejected``), so noisy
or garbled audio never reaches the embedding server.

Design notes (why this placement is cheap):

- It's a **gate**, not a transform: audio that passes is returned unchanged.
- It runs inside ``CompositeAudioProcessor``, i.e. **once per finished utterance**
  (each recognize/enroll), never in the capture loop.
- Placed **after the VAD** stage, so silent / no-speech clips are already
  cheap-rejected by VAD and never reach the (heavier) STOI model.
- The ~20 MB ONNX session is loaded **once** in ``_start_impl`` — the whole
  composite processor is a lazily-built singleton (see
  ``speaker_recognizer._get_audio_processor``), so the model isn't reloaded.

Memory: the model is a transformer over encoder frames and self-attention grows
steeply with clip length (~4.4 GB at 30 s). Audio is therefore scored in fixed
``chunk_sec`` windows and the per-chunk scores are **mean**-aggregated — each
inference is bounded to one chunk (~0.3 GB at 5 s). ONNX Runtime's CPU memory
arena is disabled so peak RSS stays flat instead of ratcheting to the worst clip.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from .base import Audio, AudioProcessorBase, gpu_lock
from .exceptions import REJECT_LOW_INTELLIGIBILITY, PreprocessRejected

# --- Defaults (calibrated on the SQUIM-STOI opt graph; see audio-metrics research) ---
DEFAULT_THRESHOLD: float = 0.75
DEFAULT_CHUNK_SEC: float = 5.0
DEFAULT_MIN_TAIL_SEC: float = 1.0  # a trailing remainder shorter than this is dropped


class SpeechIntelligibilityFilter(AudioProcessorBase):
    """STOI quality gate — reject a clip whose mean chunk STOI < ``threshold``.

    Expects mono float32 @ 16 kHz (what the pipeline produces after
    Resample + VAD). Passes audio through UNCHANGED on success.
    """

    def __init__(
        self,
        model_path: str,
        threshold: float = DEFAULT_THRESHOLD,
        chunk_sec: float = DEFAULT_CHUNK_SEC,
        expected_sample_rate: int = 16000,
    ) -> None:
        super().__init__()
        self._model_path: str = model_path
        self._threshold: float = float(threshold)
        self._chunk_sec: float = float(chunk_sec)
        self._expected_sr: int = int(expected_sample_rate)
        self._session: Any = None
        self._input_name: Optional[str] = None

    @override
    def _start_impl(self) -> None:
        if self._session is not None:
            return
        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(f"STOI model not found: {self._model_path}")

        import onnxruntime as ort  # deferred so import stays light when gate is off

        opts = ort.SessionOptions()
        # The pooled arena reserves large slabs and never returns them, so peak
        # RSS would ratchet up to the worst clip seen. Off keeps memory flat.
        opts.enable_cpu_mem_arena = False
        with gpu_lock:
            self._session = ort.InferenceSession(
                self._model_path, opts, providers=["CPUExecutionProvider"],
            )
        self._input_name = self._session.get_inputs()[0].name
        self._running = True
        self._logger.info(
            "STOI gate started (model=%s threshold=%.2f chunk=%.1fs)",
            os.path.basename(self._model_path), self._threshold, self._chunk_sec,
        )

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._input_name = None
        self._running = False
        self._logger.info("STOI gate stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    def _split_chunks(
        self, waveform: npt.NDArray[np.float32]
    ) -> list[npt.NDArray[np.float32]]:
        """Fixed ``chunk_sec`` windows (no overlap); drop a too-short trailing tail.

        Bounds each inference (and its self-attention matrix) to one chunk so a
        long clip can't OOM, and avoids scoring an unreliably-short remainder.
        """
        chunk_n = max(1, int(self._expected_sr * self._chunk_sec))
        min_tail = int(self._expected_sr * min(DEFAULT_MIN_TAIL_SEC, self._chunk_sec / 2.0))
        n = waveform.shape[0]
        chunks: list[npt.NDArray[np.float32]] = []
        for start in range(0, n, chunk_n):
            piece = waveform[start:start + chunk_n]
            if piece.shape[0] < min_tail and chunks:
                break
            chunks.append(piece)
            if start + chunk_n >= n:
                break
        return chunks or [waveform]

    def _score(self, chunk: npt.NDArray[np.float32]) -> float:
        """STOI estimate for one chunk; NaN on inference failure (→ rejects)."""
        blob = np.ascontiguousarray(chunk.reshape(1, -1), dtype=np.float32)
        try:
            with gpu_lock:
                out = self._session.run(None, {self._input_name: blob})
            return float(np.asarray(out[0]).ravel()[0])
        except Exception as exc:
            self._logger.warning("STOI inference failed on a chunk: %s", exc)
            return float("nan")

    @override
    def _process_impl(self, input: Audio) -> Audio:
        wf = input.waveform
        if wf.shape[0] == 0:
            return input  # nothing to score (VAD-empty handled upstream)

        scores = np.asarray(
            [self._score(c) for c in self._split_chunks(wf)], dtype=np.float64
        )
        finite = scores[~np.isnan(scores)]
        # NaN-aware mean; all-NaN → NaN, which rejects below.
        mean_score = float(np.mean(finite)) if finite.size else float("nan")

        # `not (mean >= thr)` so NaN (every chunk failed) also rejects.
        if not (mean_score >= self._threshold):
            duration = wf.shape[0] / float(input.sample_rate)
            raise PreprocessRejected(
                REJECT_LOW_INTELLIGIBILITY,
                input_duration_sec=duration,
                stripped_duration_sec=duration,
                stoi_score=(mean_score if not np.isnan(mean_score) else 0.0),
                stoi_threshold=self._threshold,
            )

        self._logger.debug(
            "STOI gate pass: mean=%.3f (chunks=%d)", mean_score, scores.size
        )
        return input
