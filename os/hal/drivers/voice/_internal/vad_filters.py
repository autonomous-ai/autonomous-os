"""VAD filter wrappers — WebRTC + Silero.

Each filter encapsulates its own state and exposes `is_speech(data, device_rate)`
returning True/False. Fail-open: if the underlying model isn't available, returns
True so callers don't drop legitimate speech.
"""

import logging
import threading
from math import gcd
from pathlib import Path
from typing import Optional

from hal.drivers.voice._internal.config import (
    STT_RATE,
    SILERO_CHUNK_SIZE,
    SILERO_VAD_THRESHOLD,
    WEBRTCVAD_FRAME_MS,
)

logger = logging.getLogger("hal.voice")


class WebRTCVADFilter:
    """Fast C-based VAD (~0.1ms/frame). One instance per aggressiveness level.

    Aggressiveness 0-3 (3 = most strict). The normal STT path uses aggressiveness=2;
    the barge-in path uses aggressiveness=3 to discriminate user voice from
    Device's own speaker bleed during TTS playback.
    """

    def __init__(self, aggressiveness: int, np):
        self._np = np
        self._vad = None
        try:
            import webrtcvad as _webrtcvad
            self._vad = _webrtcvad.Vad(aggressiveness)
            logger.info("WebRTC VAD loaded (aggressiveness=%d)", aggressiveness)
        except ImportError:
            logger.warning("webrtcvad not installed — pip install webrtcvad")
        except Exception as e:
            logger.warning("WebRTC VAD not available: %s", e)

    @property
    def available(self) -> bool:
        return self._vad is not None

    def is_speech(self, data, device_rate: int) -> bool:
        """Returns True if any 30ms chunk of `data` contains speech.

        Fails open (returns True) if VAD unavailable or errors — don't drop
        legitimate speech on infrastructure issues.
        """
        if self._vad is None:
            return True
        try:
            np = self._np
            if device_rate != STT_RATE:
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                audio_16k = scipy.signal.resample_poly(samples, STT_RATE // g, device_rate // g).astype(np.int16)
            else:
                audio_16k = data.flatten().astype(np.int16)
            frame_samples = int(STT_RATE * WEBRTCVAD_FRAME_MS / 1000)
            raw = audio_16k.tobytes()
            frame_bytes = frame_samples * 2
            for i in range(0, len(raw) - frame_bytes + 1, frame_bytes):
                if self._vad.is_speech(raw[i:i + frame_bytes], STT_RATE):
                    return True
            return False
        except Exception as e:
            logger.warning("WebRTC VAD error: %s", e)
            return True


class SileroVADFilter:
    """Semantic VAD (ONNX) — rejects TV, music, and other non-speech audio that
    fools energy-based VAD. Slower (~20ms/frame on ARM) so runs AFTER WebRTC.

    Stateful: maintains LSTM hidden state across calls (reset between sessions
    via `reset_state()`). Silero v5+ requires a 64-sample context prepended to
    each chunk — handled internally.
    """

    def __init__(self, model_path: Path, np):
        self._np = np
        self._session = None
        self._state = None
        self._context = None
        self._lock = threading.Lock()
        if not model_path.exists():
            logger.info("Silero VAD model not found at %s — disabled", model_path)
            return
        try:
            import os as _os
            _os.environ.setdefault("OMP_NUM_THREADS", "1")
            _os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self.reset_state()
            logger.info("Silero VAD loaded (threshold=%.2f)", SILERO_VAD_THRESHOLD)
        except Exception as e:
            logger.warning("Silero VAD not available — falling back to RMS only: %s", e)
            self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    def reset_state(self) -> None:
        """Reset LSTM hidden state + context between speech segments."""
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        # Silero v5+ requires 64 context samples (16kHz) prepended to each chunk
        self._context = np.zeros((1, 64), dtype=np.float32)

    def is_speech(self, data, device_rate: int) -> bool:
        """Run Silero on `data`. Returns True if peak confidence ≥ threshold.

        Fails open (returns True) on infrastructure errors — don't drop speech.
        """
        if self._session is None:
            return True
        try:
            np = self._np
            if device_rate != STT_RATE:
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                up, down = STT_RATE // g, device_rate // g
                audio_16k = scipy.signal.resample_poly(samples, up, down).astype(np.float32)
            else:
                audio_16k = data.flatten().astype(np.float32)

            # Normalize int16 → float32 [-1, 1]
            audio_norm = audio_16k / 32768.0

            max_conf = 0.0
            with self._lock:
                for i in range(0, len(audio_norm), SILERO_CHUNK_SIZE):
                    chunk = audio_norm[i:i + SILERO_CHUNK_SIZE]
                    if len(chunk) < SILERO_CHUNK_SIZE:
                        chunk = np.pad(chunk, (0, SILERO_CHUNK_SIZE - len(chunk)))
                    # Silero v5+: prepend 64-sample context from previous chunk
                    x = np.concatenate([self._context, chunk.reshape(1, -1)], axis=1)
                    out = self._session.run(
                        None,
                        {
                            "input": x,
                            "state": self._state,
                            "sr": np.array(STT_RATE, dtype=np.int64),
                        },
                    )
                    max_conf = max(max_conf, float(out[0][0][0]))
                    self._state = out[1]
                    self._context = x[:, -64:]

            is_speech = max_conf >= SILERO_VAD_THRESHOLD
            if not is_speech:
                logger.info("Silero: conf=%.3f < threshold=%.2f — rejected", max_conf, SILERO_VAD_THRESHOLD)
            return is_speech
        except Exception as e:
            logger.warning("Silero VAD inference error: %s", e)
            return True
