"""Piper TTS backend — synthesis on the device, no network, no quota.

Piper is an ONNX voice model driven by a small C++ binary. It runs on this
board's CPU at roughly 4x realtime, which matters twice over:

  - There is no shared rate limit to queue behind. A cloud provider caps
    concurrent requests across the whole fleet; here every unit synthesises
    its own audio, so concurrency scales with units sold and costs nothing.
  - Time-to-first-audio drops, because the network round trip disappears.
    That is the number a speaker feels, not total synthesis time.

The trade is quality: Piper is clearly behind a hosted neural voice. It is
meant as the default voice, with a hosted provider left available for users
who want the better one.

Install layout (see docs): binary at PIPER_BIN, voices in PIPER_VOICES_DIR as
<name>.onnx plus the matching <name>.onnx.json.
"""

import json
import logging
import re
import os
import shutil
import subprocess
import threading
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import TTSBackend, STREAM_CHUNK_SIZE

logger = logging.getLogger("hal.voice.tts")

PIPER_BIN = os.environ.get("HAL_PIPER_BIN", "/opt/piper/piper")
PIPER_VOICES_DIR = os.environ.get("HAL_PIPER_VOICES", "/opt/piper/voices")
PIPER_DEFAULT_VOICE = os.environ.get("HAL_PIPER_VOICE", "en_US-lessac-medium")

# Piper ships its own espeak-ng and phonemize libraries next to the binary and
# does not rpath them, so the loader needs pointing at that directory.
_PIPER_LIB_DIR = os.path.dirname(PIPER_BIN)

# Piper models are 22.05 kHz; the TTS service resamples from whatever a backend
# reports, so this is read from the model rather than forced to the 24 kHz the
# hosted providers happen to use.
_FALLBACK_SAMPLE_RATE = 22050

# Performance tags understood by hosted providers, meaningless to Piper.
# Mirrors the list in openai.py::_strip_audio_tags.
_AUDIO_TAG_RE = re.compile(
    r"\[(?:laugh|sigh|whisper|gasp|gulp|nervous|excited|frustrated|sorrowful|calm)[^\]]*\]",
    re.IGNORECASE,
)


def _default_length_scale() -> float:
    """The length scale the warm spare is spawned with. Guessing wrong only
    costs a cold start, so it follows the configured speed the service uses."""
    try:
        import hal.config as _cfg
        speed = float(getattr(_cfg, "TTS_SPEED", 1.0) or 1.0)
    except Exception:
        speed = 1.0
    return 1.0 / speed if speed > 0 else 1.0


class PiperTTSBackend(TTSBackend):
    """Local Piper synthesis, streamed as raw PCM int16."""

    def __init__(self, voice: str = "", voices_dir: str = "", binary: str = ""):
        self._bin = binary or PIPER_BIN
        self._voices_dir = voices_dir or PIPER_VOICES_DIR
        self._default_voice = voice or PIPER_DEFAULT_VOICE
        self._rate_cache: dict = {}
        self._warned_voices: set = set()
        # Voice of the utterance in flight, so sample_rate reports its rate.
        self._current_voice: str = ""
        # A pre-spawned process that has already loaded the model and is blocked
        # on stdin. Loading the 63 MB model costs ~700 ms and measurably
        # dominated time-to-first-audio for short replies ("Okay." took 775 ms,
        # of which ~700 was load). Paying it between utterances instead of
        # during one is the whole trick.
        self._spare: Optional[subprocess.Popen] = None
        self._spare_key: tuple = ()
        self._spare_lock = threading.Lock()
        if self.available:
            logger.info(
                "Piper TTS backend ready (bin=%s, voice=%s, rate=%dHz)",
                self._bin, self._default_voice, self.sample_rate,
            )
            # Warm the first process at construction, off the hot path. Without
            # this the very first thing the device says pays the model load —
            # and the first utterance after boot is the one a user judges.
            threading.Thread(
                target=self._prewarm,
                args=(self._model_path(self._default_voice), _default_length_scale()),
                daemon=True,
            ).start()
        else:
            logger.warning(
                "Piper TTS unavailable (bin=%s exists=%s, voice=%s)",
                self._bin, os.path.exists(self._bin), self._model_path(self._default_voice),
            )

    # ── discovery ────────────────────────────────────────────────────────────

    def _model_path(self, voice: str) -> str:
        """Resolve a voice name to its .onnx. Absolute paths pass through, so a
        deployment can point at a model outside the managed voices directory."""
        voice = (voice or self._default_voice).strip()
        if os.path.isabs(voice):
            return voice
        if not voice.endswith(".onnx"):
            voice += ".onnx"
        return os.path.join(self._voices_dir, voice)

    @property
    def available(self) -> bool:
        return (
            (os.path.isfile(self._bin) and os.access(self._bin, os.X_OK))
            or shutil.which(self._bin) is not None
        ) and os.path.isfile(self._model_path(self._default_voice))

    def rate_for(self, voice: str = "") -> int:
        """Sample rate of a specific model. Piper voices are not all 22.05 kHz —
        the x_low and low tiers are 16 kHz — so reading the default model's rate
        while speaking a different one would resample against the wrong source
        and shift the pitch."""
        model = self._model_path(voice)
        return self._rate_of(model)

    @property
    def sample_rate(self) -> int:
        """Rate of the currently-selected voice, for the service's resampler."""
        return self._rate_of(self._model_path(self._current_voice or self._default_voice))

    def _rate_of(self, model: str) -> int:
        if model in self._rate_cache:
            return self._rate_cache[model]
        rate = _FALLBACK_SAMPLE_RATE
        try:
            with open(model + ".json", "r", encoding="utf-8") as fh:
                rate = int(json.load(fh)["audio"]["sample_rate"])
        except Exception as e:  # missing or malformed sidecar
            logger.warning("Piper: cannot read sample rate from %s.json (%s), assuming %d",
                           model, e, rate)
        self._rate_cache[model] = rate
        return rate

    @property
    def volume_boost(self) -> float:
        """1.0, not the 2.5 the hosted backends use. Piper already peaks at full
        scale (measured: peak 1.00, RMS 0.13), so any boost is pure clipping —
        the service multiplies then clips, so 2.5 would flat-top every vowel."""
        return 1.0

    # ── process pool of one ──────────────────────────────────────────────────

    def _spawn(self, model_path: str, length_scale: float) -> subprocess.Popen:
        """Start Piper and let it load the model. It then blocks reading stdin,
        so the returned process is idle and ready to synthesise immediately."""
        env = dict(os.environ)
        if _PIPER_LIB_DIR:
            env["LD_LIBRARY_PATH"] = (
                _PIPER_LIB_DIR + os.pathsep + env.get("LD_LIBRARY_PATH", "")
            ).rstrip(os.pathsep)
        return subprocess.Popen(
            [self._bin, "--model", model_path, "--output-raw",
             "--length_scale", f"{length_scale:.3f}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )

    def _prewarm(self, model_path: str, length_scale: float) -> None:
        key = (model_path, round(length_scale, 3))
        try:
            proc = self._spawn(*key)
        except Exception as e:
            logger.warning("Piper prewarm failed: %s", e)
            return
        with self._spare_lock:
            # Another prewarm won the race, or the voice changed underneath us.
            if self._spare is not None:
                proc.kill()
                return
            self._spare, self._spare_key = proc, key

    def _take_process(self, model_path: str, length_scale: float) -> subprocess.Popen:
        """Hand out the warm process if it matches, then immediately start its
        replacement so the next utterance is warm too. Length scale is a command
        line argument, so a spare is only reusable at the same speed."""
        key = (model_path, round(length_scale, 3))
        proc = None
        with self._spare_lock:
            if self._spare is not None and self._spare_key == key and self._spare.poll() is None:
                proc, self._spare = self._spare, None
        if proc is None:
            proc = self._spawn(*key)
        threading.Thread(target=self._prewarm, args=key, daemon=True).start()
        return proc

    # ── synthesis ────────────────────────────────────────────────────────────

    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Yield raw PCM int16 chunks as Piper produces them.

        `model` and `instructions` are ignored: a Piper voice IS the model, and
        there is no prompt channel to style it. `voice` selects the .onnx.
        """
        # Strip the ElevenLabs-style performance tags the rest of the stack
        # emits ("[laugh]", "[whisper]"). A hosted model interprets them; Piper
        # has no such channel and would read the word "laugh" out loud, which
        # is exactly how a correct voice comes to sound broken.
        text = _AUDIO_TAG_RE.sub("", (text or "")).strip()
        if not text:
            return

        self._current_voice = voice or self._default_voice
        model_path = self._model_path(voice)
        if not os.path.isfile(model_path):
            # Once per distinct name, not once per utterance. A device
            # configured with a hosted provider's voice name ("Rachel") hits
            # this on every sentence, and the warning drowns the log it is
            # supposed to help with — /var/log is a RAM disk here.
            if voice not in self._warned_voices:
                self._warned_voices.add(voice)
                logger.warning("Piper: voice %r not found at %s, using %s instead",
                               voice, model_path, self._default_voice)
            model_path = self._model_path(self._default_voice)
            self._current_voice = self._default_voice

        # Piper expresses tempo as length scale — the inverse of speed. Guard
        # against a zero or negative speed reaching the process as a divide.
        length_scale = 1.0 / speed if speed and speed > 0 else 1.0

        proc = self._take_process(model_path, length_scale)

        # Drain stderr on a thread. Piper logs its realtime factor per line and
        # will block on a full pipe if nobody reads it — which would stall
        # synthesis mid-sentence and look like a hang.
        stderr_tail: list = []

        def _drain():
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_tail.append(line.decode("utf-8", "replace").rstrip())
                    del stderr_tail[:-5]
            except Exception:
                pass

        threading.Thread(target=_drain, daemon=True).start()

        try:
            # One utterance per process: close stdin so Piper knows the input is
            # complete and flushes the tail of the audio.
            proc.stdin.write(text.encode("utf-8") + b"\n")  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]

            while True:
                chunk = proc.stdout.read(STREAM_CHUNK_SIZE)  # type: ignore[union-attr]
                if not chunk:
                    break
                yield chunk
        except BrokenPipeError:
            logger.warning("Piper: process closed early: %s", "; ".join(stderr_tail))
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass
            # The consumer may abandon the generator mid-utterance (barge-in),
            # so never wait indefinitely on a process nobody is reading.
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            # A negative return code means a signal, which here always means we
            # sent it — barge-in killing a half-spoken sentence, or shutdown
            # reaping the warm spare. Neither is a fault worth a warning.
            if proc.returncode not in (0, None) and proc.returncode > 0 and stderr_tail:
                logger.warning("Piper exited %s: %s", proc.returncode, "; ".join(stderr_tail))
