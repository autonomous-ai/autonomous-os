"""Speaker voice recognition service.

Stores per-user voice embeddings under ``/root/local/users/<name>/voice/`` and
recognizes speakers via cosine similarity. Embeddings are computed by a
configurable external API (see ``SPEAKER_EMBEDDING_API_URL``).

Audio preprocessing (Mono → Resample → [HighPass] → [NoiseReduce] → VAD →
[STOI gate] → RMS) runs ON THIS DEVICE (see ``audio_processors/``), next to the
mic. Only audio that passes the VAD + STOI intelligibility gate is uploaded; the
server is told to skip its own preprocessing (``preprocess=false``) and just
compute the embedding.

External API contract:
    POST {SPEAKER_EMBEDDING_API_URL}
    Headers: X-API-Key: {SPEAKER_EMBEDDING_API_KEY} (optional)
    Body:    {"audios_b64": ["<base64 WAV>", ...], "preprocess": false,
              "use_sliding_window": <bool>}
      ``use_sliding_window`` picks the chunking policy: false (enroll) embeds the
      whole utterance in a single shot; true (recognize) slides overlapping
      windows and returns the per-chunk matrix for voting.
    Response: {"embedding": [float, ...],            # 1-D aggregate, any dim
               "chunk_embeddings": [[float, ...]]}   # [M, D], only when
                                                     # use_sliding_window=true

A speaker is stored as a BANK of per-sample embeddings — one row per WAV, never
averaged together. Retrieval takes the max over a speaker's rows, mirroring the
face pipeline (``faceid/recognizer.py``). Averaging was removed because it made
every sample able to damage every other: one bad clip shifted the single stored
vector, so samples had to be filtered and deleted to protect the mean, and a
merge decision could not be undone.

Two tiers per user, kept separate exactly as face keeps uploads vs extended:

* **anchor** — audio the user deliberately enrolled. Permanent; no automatic
  path prunes or deletes it.
* **extended** — auto-captured: unknown-cluster audio claimed at enroll time,
  plus confidently-recognized later turns. Capped and diversity-pruned.

Storage layout per user::

    /root/local/users/<norm>/
        metadata.json           — SHARED identity (telegram_username, telegram_id,
                                   display_name). Same file face-enroll writes —
                                   merged on write, never overwritten blindly.
        voice/
            metadata.json       — voice-specific (enrolled_at, updated_at,
                                   num_samples, sample_files, embedding_dim)
            sample_<origin>_<ts>_<uuid>.wav  — anchor WAV (16kHz mono)
            sample_<origin>_<ts>_<uuid>.npy  — its L2-normalized embedding [D]
            .extended/
                ext_<ts>_<seq>.wav           — auto-captured sample
                ext_<ts>_<seq>.npy           — its embedding [D]
            embedding.npy       — LEGACY aggregated vector. No longer written;
                                   still READ as a one-row bank so profiles from
                                   before the rewrite keep matching.

Label normalization matches :class:`FaceRecognizer.normalize_label` so face /
voice / mood / wellbeing all share the same per-user folder for a person.

Registry of users with registered voices::

    /root/local/users/.voice_registry.json
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import wave
from math import gcd
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import requests

from hal import config
from hal.drivers.sensing.crypto import CryptoSession, resolve_public_key

logger = logging.getLogger("hal.voice.speaker")

# --- Storage layout (paths come from hal.config) ---
_USERS_DIR = Path(config.USERS_DIR)
_VOICE_SUBDIR = "voice"
# LEGACY single aggregated vector. No longer written — kept as a read-only
# fallback so profiles enrolled before the bank rewrite keep matching as a
# one-row bank instead of silently un-enrolling. See _load_user_bank.
_EMBEDDING_FILE = "embedding.npy"
_METADATA_FILE = "metadata.json"
_REGISTRY_FILE = _USERS_DIR / ".voice_registry.json"
_UNKNOWN_AUDIO_DIR = Path(config.SPEAKER_UNKNOWN_AUDIO_DIR)
_MAX_INCOMING_FILES = config.SPEAKER_MAX_INCOMING_FILES

# Each stored WAV carries a sidecar .npy holding its (already L2-normalized)
# embedding, so a reload never has to re-run inference on a clip the current
# preprocessing gate might now reject — the very samples worth keeping are the
# ones most likely to fail a re-gate. Mirrors faceid/recognizer.py.
_SIDECAR_EXT = ".npy"
# Auto-captured "extended" samples live in this per-user subfolder, i.e.
# <user>/voice/.extended/. Dot-prefixed so the sample loader (which globs
# sample_*.wav directly under voice/) never mistakes one for an enrollment
# sample, and so the web UI's voice-file listing stays clean.
_EXTENDED_SUBDIR = ".extended"
_EXTENDED_PREFIX = "ext_"

# Where an enrollment sample came from, embedded in its filename. Single tokens
# with no "_", because _sample_origin recovers the tag by splitting on "_".
# "other" is the catch-all for anything a caller sends that is not in this set.
_SAMPLE_ORIGINS = ("mic", "telegram", "web", "other")

# --- External embedding API (centralized in hal.config) ---
_API_URL = config.SPEAKER_EMBEDDING_API_URL
_API_KEY = config.SPEAKER_EMBEDDING_API_KEY
_API_TIMEOUT_S = config.SPEAKER_EMBEDDING_API_TIMEOUT_S

# --- Voice stranger clustering ---
# Assigns a stable "voiceprint_hash" (voice_<N> label) to every unknown voice
# so callers can track "same unknown speaker seen multiple times" without
# needing voiceprint_hash support from the embedding backend. Mirrors the
# face stranger tracker in faceid/perception.py.
_VOICE_STRANGERS_DIR = Path(
    os.environ.get("HAL_VOICE_STRANGERS_DIR", "/root/local/voice_strangers")
)
# Cap cluster COUNT (not row count) so disk doesn't grow unbounded. Oldest
# cluster evicted first, and its on-disk dir goes with it — see
# _evict_oldest_clusters. Each cluster holds up to _MAX_CLUSTER_SAMPLES rows.
_MAX_VOICE_STRANGERS = int(
    os.environ.get("HAL_MAX_VOICE_STRANGERS", "50")
)
_VOICE_STRANGER_PREFIX = "voice_"
_VOICE_STRANGER_DIR_RE = re.compile(r"^voice_\d+$")

# --- Identity thresholds ---
# EVERY similarity in this file is RAW cosine in [-1, 1] — embeddings are
# L2-normalized, so `a @ b` IS the cosine and no rescaling happens anywhere.
# (Before the bank rewrite these were SCALED cosine, `(raw + 1) / 2`; the
# config names changed with the unit so a stale scaled value cannot be reread
# as raw. Conversion: raw = 2 * scaled - 1.)
#
# One identity bar is used for every identity question — recognizing an
# enrolled user, deciding a returning unknown voice belongs to an existing
# cluster, deciding an unknown cluster's audio belongs to the person being
# enrolled, and deciding two clips in one enroll batch are the same person. There is deliberately no looser merge gate: the old one admitted
# clips at 0.625 scaled that were then used to judge genuine enroll audio at
# 0.75 scaled.
_MATCH_COS = config.SPEAKER_MATCH_COS
# Redundancy, NOT identity — a different axis, hence a different number. Must
# stay above _MATCH_COS: both gates measure max cosine to the user's existing
# samples, so the extended set admits exactly (_MATCH_COS, _DIVERSITY_COS].
_DIVERSITY_COS = config.SPEAKER_DIVERSITY_COS
_MAX_EXTENDED_SAMPLES = config.SPEAKER_MAX_EXTENDED_SAMPLES
_MAX_CLUSTER_SAMPLES = config.SPEAKER_MAX_CLUSTER_SAMPLES
_MAX_CLUSTER_FILES = config.SPEAKER_MAX_CLUSTER_FILES
_EXTEND_MIN_DURATION_S = config.SPEAKER_EXTEND_MIN_DURATION_SEC
_EXTEND_MIN_MARGIN_COS = config.SPEAKER_EXTEND_MIN_MARGIN_COS

# Target sample rate for stored/enrolled audio (matches STT pipeline).
_TARGET_SR = 16000


class SpeakerRecognizerError(Exception):
    """Raised on invalid input or external API failure."""


class EmbeddingAPIUnavailableError(SpeakerRecognizerError):
    """Raised when the embedding API is unreachable / 5xx / protocol-broken.

    Distinct from audio-level rejections: the audio itself may be perfectly
    fine — the caller should retry rather than ask the user to re-record.
    Callers that batch over multiple samples MUST abort on this error
    instead of skipping the sample, to avoid misattributing an outage to
    bad audio and to avoid destructive cleanup of valid on-disk samples.
    """


# ---------------------------------------------------------------------------
# On-device audio preprocessing (moved from perception-service).
#
# The filter/VAD/normalize chain (Mono -> Resample -> [HighPass] ->
# [NoiseReduce] -> VAD -> [STOI gate] -> RMS) now runs HERE, next to the mic.
# Only audio that passes the VAD + STOI intelligibility gate is uploaded; the
# embedding server is then told to skip its own preprocessing (preprocess=false)
# and just compute the embedding. The composite processor is a lazily-built
# singleton because the TEN-VAD + STOI models load once and are reused across
# every enroll/recognize call.
# ---------------------------------------------------------------------------
_audio_processor: Optional[Any] = None
_audio_processor_lock = threading.Lock()


def _get_audio_processor() -> Any:
    """Lazily build + start the composite preprocessor (TEN-VAD loads once)."""
    global _audio_processor
    if _audio_processor is not None:
        return _audio_processor
    with _audio_processor_lock:
        if _audio_processor is None:
            # Heavy imports (onnxruntime for TEN-VAD + STOI, noisereduce) are
            # deferred to first use so module import stays light and lint
            # doesn't require the VAD deps.
            from hal.drivers.voice.speaker_recognizer.audio_processors.factory import (
                AudioProcessorFactory,
            )

            factory = AudioProcessorFactory(
                target_sample_rate=config.SPEAKER_PROC_TARGET_SR,
                enable_mono=config.SPEAKER_PROC_ENABLE_MONO,
                enable_resample=config.SPEAKER_PROC_ENABLE_RESAMPLE,
                enable_high_pass=config.SPEAKER_PROC_ENABLE_HIGH_PASS,
                high_pass_cutoff_hz=config.SPEAKER_PROC_HIGH_PASS_CUTOFF_HZ,
                enable_noise_reduce=config.SPEAKER_PROC_ENABLE_NOISE_REDUCE,
                noise_reduce_stationary=config.SPEAKER_PROC_NOISE_STATIONARY,
                enable_vad=config.SPEAKER_PROC_ENABLE_VAD,
                vad_min_duration_sec=config.SPEAKER_PROC_VAD_MIN_DURATION_SEC,
                vad_min_voice_ratio=config.SPEAKER_PROC_VAD_MIN_VOICE_RATIO,
                vad_speech_prob_threshold=config.SPEAKER_PROC_VAD_SPEECH_PROB_THRESHOLD,
                vad_speaker_band=config.SPEAKER_PROC_VAD_SPEAKER_BAND,
                vad_max_level_drop_db=config.SPEAKER_PROC_VAD_MAX_LEVEL_DROP_DB,
                enable_rms_normalize=config.SPEAKER_PROC_ENABLE_RMS_NORMALIZE,
                rms_target=config.SPEAKER_PROC_RMS_TARGET,
                enable_stoi=config.SPEAKER_PROC_ENABLE_STOI,
                stoi_model_path=config.SPEAKER_PROC_STOI_MODEL_PATH,
                stoi_threshold=config.SPEAKER_PROC_STOI_THRESHOLD,
                stoi_chunk_sec=config.SPEAKER_PROC_STOI_CHUNK_SEC,
            )
            try:
                proc = factory.create()
                proc.start()  # loads the TEN-VAD ONNX model
            except Exception as e:
                # Missing dep / model-load failure is systemic, not audio-level.
                # Raise EmbeddingAPIUnavailableError so enroll aborts cleanly
                # (never deletes on-disk samples) and recognize degrades to
                # "unknown" instead of crashing every turn with a 500.
                logger.error(
                    "Failed to init on-device audio preprocessor: %s", e)
                raise EmbeddingAPIUnavailableError(
                    f"audio preprocessor unavailable: {e}"
                ) from e
            _audio_processor = proc
            logger.info(
                "On-device audio preprocessor ready "
                "(mono=%s resample=%s highpass=%s noise=%s vad=%s stoi=%s rms=%s)",
                config.SPEAKER_PROC_ENABLE_MONO, config.SPEAKER_PROC_ENABLE_RESAMPLE,
                config.SPEAKER_PROC_ENABLE_HIGH_PASS, config.SPEAKER_PROC_ENABLE_NOISE_REDUCE,
                config.SPEAKER_PROC_ENABLE_VAD, config.SPEAKER_PROC_ENABLE_STOI,
                config.SPEAKER_PROC_ENABLE_RMS_NORMALIZE,
            )
    return _audio_processor


def _normalize_label(name: str) -> str:
    """Folder-safe lowercase label — matches FaceRecognizer.normalize_label.

    Keeping this rule identical to the face recognizer ensures that a person
    enrolled via face and via voice lands in the SAME per-user folder, and
    that mood/wellbeing/music-suggestion logs all refer to the same identity.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = s.strip("_")
    return s[:64] if s else "person"


def _cosine_similarity(e1: np.ndarray, e2: np.ndarray) -> float:
    """Raw cosine similarity in [-1, 1].

    Tolerates non-normalized inputs (unlike plain ``np.dot`` which requires
    pre-normalized vectors). The ``+ 1e-12`` guards against zero-norm inputs.
    This value is compared DIRECTLY against the thresholds in this file — there
    is no [0, 1] rescaling step anywhere.
    """
    return float(
        np.dot(e1, e2) / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-12)
    )


def _l2(vec: np.ndarray) -> np.ndarray:
    """L2-normalize; return unchanged if norm is ~0."""
    arr = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if n < 1e-12:
        return arr
    return (arr / n).astype(np.float32)


def _select_diverse(
    candidates: np.ndarray, anchor: Optional[np.ndarray], k: int
) -> list[int]:
    """Greedy farthest-point selection: indices of the ``k`` most diverse rows.

    Ported from ``faceid/recognizer.py::_select_diverse``. Starting from
    ``anchor`` (the user's permanent enrollment samples) as reference points,
    repeatedly keep the candidate whose similarity to everything already kept is
    LOWEST — the most novel sample. This packs the capped slots with audio that
    COMPLEMENTS the enrollment (different distance, loudness, room) rather than
    more of the same. If ``anchor`` is None/empty the newest candidate seeds it.

    Caveat worth knowing when tuning: in face space the dominant within-person
    axis is pose, so "farthest" means "useful new angle". In speaker space the
    dominant axis is channel and noise, so farthest-point will happily rank a
    degraded clip as the most valuable one. The identity floor (a candidate must
    clear _MATCH_COS to be considered at all) plus the duration gate in
    _maybe_extend_user are what keep that in check — not this function.
    """
    m = len(candidates)
    if m <= k:
        return list(range(m))

    if anchor is not None and len(anchor):
        selected_ref: list[np.ndarray] = [anchor]
        selected_local: list[int] = []
    else:
        seed = m - 1  # newest candidate
        selected_ref = [candidates[seed][None, :]]
        selected_local = [seed]

    remaining = [j for j in range(m) if j not in selected_local]
    while len(selected_local) < k and remaining:
        ref = np.concatenate(selected_ref)          # [K, D]
        sims = candidates[remaining] @ ref.T        # [R, K] raw cosine
        nearest = sims.max(axis=1)
        pick = int(np.argmin(nearest))
        chosen = remaining.pop(pick)
        selected_local.append(chosen)
        selected_ref.append(candidates[chosen][None, :])
    return selected_local


def _sidecar_path(wav_path: Path) -> Path:
    """The ``.npy`` embedding stored next to a sample WAV."""
    return wav_path.with_suffix(_SIDECAR_EXT)


def _sample_origin(filename: str) -> str:
    """Parse the origin tag encoded in ``sample_<origin>_<ts>_<uuid>.wav``.

    Legacy files ``sample_<ts>_<uuid>.wav`` (no origin) → ``"unknown"``.
    """
    parts = filename.split("_", 2)
    if len(parts) >= 2 and parts[0] == "sample":
        candidate = parts[1]
        if candidate in _SAMPLE_ORIGINS:
            return candidate
    return "unknown"


def _merge_shared_metadata(
    user_dir: Path,
    *,
    display_name: str | None = None,
    telegram_username: str | None = None,
    telegram_id: str | None = None,
) -> dict[str, Any]:
    """Merge identity fields into ``/root/local/users/<norm>/metadata.json``.

    This is the SAME file that :class:`FaceRecognizer` writes — we read,
    update only the provided fields, and write back. Empty/``None`` values
    never overwrite existing entries.
    """
    path = user_dir / "metadata.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            data = {}
    if display_name:
        data.setdefault("display_name", display_name)
    if telegram_username:
        data["telegram_username"] = telegram_username
    if telegram_id:
        data["telegram_id"] = telegram_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as e:
        logger.warning("failed to write shared metadata %s: %s", path, e)
    return data


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _wav_duration_s(wav_bytes: bytes) -> float:
    """Best-effort duration in seconds. 0.0 when the clip cannot be read.

    Read from the RIFF header, which already carries the frame count and rate.
    This used to decode the whole file through
    ``_wav_bytes_to_float32_16k_mono`` and divide by the target rate — correct,
    but it paid a full decode plus a ``resample_poly`` pass to count samples.
    On a 44.1 kHz clip (a Telegram voice note) that was ~5 ms to answer "how
    long is this"; the header answers it in ~8 us with an identical result.

    Falls back to the decode for anything ``wave`` cannot parse, so a container
    it does not understand still gets a real answer instead of 0.0.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            if rate > 0:
                return float(wf.getnframes()) / float(rate)
    except Exception:
        pass
    try:
        return float(
            _wav_bytes_to_float32_16k_mono(wav_bytes).shape[0]
        ) / _TARGET_SR
    except Exception:
        return 0.0


def _wav_bytes_to_float32_16k_mono(raw: bytes) -> np.ndarray:
    """Decode WAV bytes into float32 mono waveform at 16kHz."""
    try:
        import soundfile as sf  # type: ignore
    except ImportError as e:
        raise SpeakerRecognizerError(
            "soundfile is required for WAV processing"
        ) from e

    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32")
    except Exception as e:
        raise SpeakerRecognizerError(f"cannot decode WAV: {e}") from e

    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    elif arr.ndim != 1:
        raise SpeakerRecognizerError(f"unsupported WAV shape {arr.shape}")

    if sr != _TARGET_SR:
        try:
            from scipy.signal import resample_poly  # type: ignore
        except ImportError as e:
            raise SpeakerRecognizerError(
                "scipy is required for resampling"
            ) from e
        g = gcd(_TARGET_SR, int(sr))
        arr = resample_poly(arr, _TARGET_SR // g, int(sr) // g).astype(np.float32)
    return arr


def _float32_waveform_to_wav_bytes(
    waveform: np.ndarray, sample_rate: int = _TARGET_SR
) -> bytes:
    """Encode a float32 mono waveform into PCM_16 WAV bytes (16kHz by default).

    ``sample_rate`` is only ever passed by the SPEAKER-DEBUG partial-chain dump,
    which can capture a waveform from *before* the Resampler stage ran.
    """
    try:
        import soundfile as sf  # type: ignore
    except ImportError as e:
        raise SpeakerRecognizerError(
            "soundfile is required for WAV processing"
        ) from e
    buf = io.BytesIO()
    sf.write(buf, np.asarray(waveform, dtype=np.float32), int(sample_rate), format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _ensure_wav_16k_mono(raw: bytes) -> bytes:
    """Normalize WAV bytes to 16kHz mono PCM_16 WAV bytes."""
    return _float32_waveform_to_wav_bytes(_wav_bytes_to_float32_16k_mono(raw))


def pcm16_bytes_to_wav(pcm_bytes: bytes, sample_rate: int = _TARGET_SR) -> bytes:
    """Wrap raw int16 mono PCM bytes in a WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


# ==========================================================================
# ==  SPEAKER-DEBUG  —  TEMPORARY DIAGNOSTIC TRACER  —  REMOVE BEFORE DEPLOY ==
# ==========================================================================
# Everything tagged `SPEAKER-DEBUG` in this file is a throwaway diagnostic aid
# for tuning speaker recognition. It traces every recognize()/enroll() call to
# disk (input audio, query/enroll embeddings, full cosine-similarity breakdown,
# reject reason, or server error), mirroring the facial-emotion debug logs.
#
# TO REMOVE FOR PRODUCTION: delete this block and every line/call marked
# `SPEAKER-DEBUG` (grep -n "SPEAKER-DEBUG" this file — the __init__ line and the
# self._debug.* calls in recognize()/enroll()). It is self-contained: no other
# module or config file is touched. It is OFF by default (production-safe); set
# HAL_SPEAKER_DEBUG=true to enable it during development.
#
# Env knobs (all optional):
#   HAL_SPEAKER_DEBUG            "true" to enable (OFF by default)
#   HAL_SPEAKER_DEBUG_DIR        output root (default: ./speaker_logs next to this file)
#   HAL_SPEAKER_DEBUG_MAX_ENTRIES  per-kind dir cap, oldest pruned (default 1000; 0=unbounded)
#
# Layout (per call, named like the facial-emotion logs); <root> defaults to the
# `speaker_logs/` folder beside this file for fast inspection:
#   <root>/recognize/<ts>_<class>_<confidence>/       (class = enrolled name | stranger-<N> | unknown)
#   <root>/recognize/<ts>_FAIL-<reason>/              (too-short | too-silent | server-error | ...)
#   <root>/enroll/<ts>_<norm>_<cohesion>/             (cohesion = mean pairwise sim among the stored anchors)
#   <root>/enroll/<ts>_FAIL-<reason>/
# each dir holds: input.wav (raw) + preprocessed.wav (post VAD/STOI/RMS, what was
# uploaded) / sample_NN.wav, *.npy embeddings, result.json + profile.json.
# result.json carries the recognition decision plus the preprocessing metrics
# (incl. STOI score) and, on a gate reject, the reason; profile.json carries ONLY
# the per-stage latency + memory numbers, kept separate so neither file buries
# the other.
# NOTE: speaker_logs/ lands inside the source tree — don't commit it (the whole
# SPEAKER-DEBUG block is meant to be removed before deploy anyway).
def _debug_audio_stats(wav_bytes: bytes) -> tuple[Optional[float], Optional[float]]:
    """Best-effort (duration_s, rms) from WAV bytes for the trace. Never raises."""
    try:
        wf = _wav_bytes_to_float32_16k_mono(wav_bytes)
        dur = round(float(wf.shape[0]) / _TARGET_SR, 3)
        rms = round(float(np.sqrt(np.mean(wf.astype(np.float64) ** 2))), 6)
        return dur, rms
    except Exception:
        return None, None


def _debug_stranger_label(vp_hash: Optional[str]) -> str:
    """SPEAKER-DEBUG: 'voice_3' -> 'stranger-3' for readable trace-dir names.

    The internal id stays 'voice_<N>' everywhere (cluster folders, result.json
    voiceprint_hash); only the human-facing debug dir token is rewritten.
    """
    if not vp_hash:
        return "unknown"
    return vp_hash.replace(_VOICE_STRANGER_PREFIX, "stranger-", 1)


# ---------------------------------------------------------------------------
# SPEAKER-DEBUG: per-stage latency / CPU / memory profiler.
#
# Rides on the SAME trace the tracer already writes — same per-call dir, same
# HAL_SPEAKER_DEBUG switch, no extra env knob — but lands in its own
# `profile.json` rather than in result.json, which is already dense with the
# recognition decision.
#
# Stages form a TREE: a stage opened inside another becomes its child, so the
# containment is structural instead of a naming convention the reader has to
# parse. `preprocess` owns `ten_vad` / `stoi_gate`; summing the top level
# gives the call total without double-counting:
#
#   decode_input                 base64/file read + WAV normalize to 16k mono
#   preprocess                   the whole on-device chain
#     +- decode_wav                WAV bytes -> float32 waveform
#     +- processor_init            lazy build/start of the composite (models load)
#     +- mono / resample / high_pass / noise_reduce
#     +- ten_vad                 << the TEN-VAD stage, profiled explicitly
#     +- stoi_gate               << the STOI gate, profiled explicitly
#     +- rms_normalize
#     +- encode_wav                cleaned waveform -> WAV bytes -> base64
#   embed_api                    the embedding call
#     +- request                 << the HTTP request itself
#     +- decode                    response parse + L2 normalize
#   load_enrolled / match_vote / stranger_cluster / save_input_wav
#
# Each node also carries `self_ms` (its own time minus its children's), so the
# cost of a parent's own glue is visible rather than hidden in the total.
#
# MEMORY. RSS is SAMPLED on a background thread (~20 ms) for the life of the
# call, and each stage reports the PEAK seen inside its own window. Endpoint-only
# sampling — read RSS at entry, read it again at exit — was the original approach
# and it is wrong for this pipeline: RSS moves only when the allocator asks the
# OS for pages or returns them, so a stage that allocates and frees within its
# own window reports 0.0, and a stage that happens to run when an earlier
# allocation is released reports a NEGATIVE cost. Real traces showed exactly
# that — `stoi_gate` at 1.7 s of ONNX inference reporting 0.00 MB on one call
# and -28.62 MB on the next. So three numbers are kept, each answering a
# different question:
#
#   rss_peak_delta_mb   peak-during-stage minus RSS at entry — "what did this
#                       stage cost at its worst". THIS is the memory number to
#                       read; it survives allocate-then-free.
#   rss_end_delta_mb    exit minus entry — "what did it keep". Legitimately
#                       negative when the allocator hands pages back.
#   rss_peak_mb         absolute high-water RSS during the stage.
#
# Caveat that no RSS-based method escapes: RSS is PROCESS-wide, so a concurrent
# HAL thread allocating during a stage lands in that stage's numbers. Sampling
# widens that exposure (it sees every spike in the window, not just the
# endpoints) in exchange for catching transient peaks at all. Read a single
# stage's memory as an upper bound, and prefer the shape across several calls.
#
# CPU. `cpu_ms` is process CPU time (`time.process_time()`) across the whole
# process, which is what catches the ONNX/torch intra-op thread pools — the
# work that makes `stoi_gate` expensive. `cpu_pct` is cpu_ms/wall_ms*100, so
# >100% means it used more than one core and ~0% means it was blocked, not
# working (`embed_api.request` should sit near zero — it is waiting on the
# network). `thread_cpu_ms` is the calling thread alone, so the gap between it
# and `cpu_ms` is roughly what the pools did. Both are process-wide in the same
# way RSS is: another busy HAL thread inflates `cpu_ms`.
#
# The RSS source is recorded as `rss_source`, because it changes what a delta means:
#
#   "psutil" / "statm"  CURRENT RSS — the accurate case (on-device Linux, and
#                       any box with psutil). A delta can be NEGATIVE when the
#                       allocator hands pages back.
#   "rusage"            HIGH-WATER RSS (ru_maxrss) — the macOS-without-psutil
#                       fallback. Monotonic, so deltas are growth-only: a stage
#                       reports > 0 only when it pushed the process past its
#                       previous peak. Good enough to spot which stage owns the
#                       footprint, useless for measuring memory being released.
#
# Repeated stages (enroll embeds many samples) are summed, with `calls` and
# `ms_max` alongside.
from contextlib import contextmanager, nullcontext  # SPEAKER-DEBUG

# Resolved once on first sample: "psutil" | "statm" | "rusage" | "none".
_debug_rss_mode: Optional[str] = None
_debug_rss_proc: Any = None


def _debug_rss_bytes() -> Optional[int]:
    """SPEAKER-DEBUG: process RSS in bytes; None if unmeasurable.

    See the block comment above for what each backend actually measures —
    ``rusage`` is a high-water mark, not current RSS.
    """
    global _debug_rss_mode, _debug_rss_proc
    if _debug_rss_mode is None:
        try:
            import psutil  # optional; present on-device via the HAL deps

            _debug_rss_proc = psutil.Process()
            _debug_rss_mode = "psutil"
        except Exception:
            if os.path.exists("/proc/self/statm"):
                _debug_rss_mode = "statm"
            else:
                _debug_rss_mode = "rusage"
    try:
        if _debug_rss_mode == "psutil":
            return int(_debug_rss_proc.memory_info().rss)
        if _debug_rss_mode == "statm":
            # field 1 = resident pages
            with open("/proc/self/statm", "r") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if _debug_rss_mode == "rusage":
            import resource
            import sys

            maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is bytes on Darwin/BSD, kilobytes on Linux.
            return int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
    except Exception:
        return None
    return None


def _debug_peak_rss_bytes() -> Optional[int]:
    """SPEAKER-DEBUG: process high-water RSS (VmHWM). Linux only, else None."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def _debug_mb(n: Optional[float]) -> Optional[float]:
    """SPEAKER-DEBUG: bytes -> MB, rounded. Passes None through."""
    return None if n is None else round(float(n) / (1024.0 * 1024.0), 2)


# SPEAKER-DEBUG: processor class -> stage name. The two gates the profile is
# really about are named after the model (ten_vad / stoi_gate), not the class —
# so this key changes when the model behind a stage does. `ten_vad` was
# `silero_vad` before the TEN-VAD swap; traces predating it use the old name.
_DEBUG_STAGE_NAMES: dict[str, str] = {
    "MonoConverter": "mono",
    "Resampler": "resample",
    "HighPassFilter": "high_pass",
    "NoiseReducer": "noise_reduce",
    "VoiceActivityFilter": "ten_vad",
    "SpeechIntelligibilityFilter": "stoi_gate",
    "RMSNormalizer": "rms_normalize",
}


class _StageNode:
    """SPEAKER-DEBUG: one node in the stage tree. Children nest under parents."""

    __slots__ = (
        "name", "calls", "ms", "ms_max", "cpu_ms", "thread_cpu_ms",
        "rss_peak_b", "rss_peak_delta_b", "rss_end_delta_b", "rss_after_b",
        "children", "_order",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.ms = 0.0
        self.ms_max = 0.0
        self.cpu_ms = 0.0
        self.thread_cpu_ms = 0.0
        self.rss_peak_b: Optional[int] = None
        self.rss_peak_delta_b: Optional[int] = None
        self.rss_end_delta_b: Optional[int] = None
        self.rss_after_b: Optional[int] = None
        self.children: dict[str, "_StageNode"] = {}
        self._order: list[str] = []

    def child(self, name: str) -> "_StageNode":
        """Get-or-create a child, preserving first-seen (pipeline) order."""
        node = self.children.get(name)
        if node is None:
            node = _StageNode(name)
            self.children[name] = node
            self._order.append(name)
        return node

    def kids(self) -> list["_StageNode"]:
        return [self.children[n] for n in self._order]

    def accumulate(
        self,
        ms: float,
        cpu_ms: float,
        thread_cpu_ms: float,
        rss_before: Optional[int],
        rss_after: Optional[int],
        rss_peak: Optional[int],
    ) -> None:
        self.calls += 1
        self.ms += ms
        self.ms_max = max(self.ms_max, ms)
        self.cpu_ms += cpu_ms
        self.thread_cpu_ms += thread_cpu_ms
        if rss_after is not None:
            self.rss_after_b = rss_after
            if rss_before is not None:
                self.rss_end_delta_b = (
                    (self.rss_end_delta_b or 0) + (rss_after - rss_before)
                )
        if rss_peak is not None:
            self.rss_peak_b = (
                rss_peak if self.rss_peak_b is None else max(self.rss_peak_b, rss_peak)
            )
            if rss_before is not None:
                # MAX, not sum: for a repeated stage the useful number is the
                # worst single occurrence ("how big did one call of this get"),
                # not a total that grows with the sample count.
                growth = max(0, rss_peak - rss_before)
                self.rss_peak_delta_b = (
                    growth if self.rss_peak_delta_b is None
                    else max(self.rss_peak_delta_b, growth)
                )

    def to_dict(self) -> dict[str, Any]:
        kids = self.kids()
        d: dict[str, Any] = {
            "stage": self.name,
            "ms": round(self.ms, 2),
            # This node's OWN time — the parent's glue, not its children's work.
            "self_ms": round(max(0.0, self.ms - sum(k.ms for k in kids)), 2),
            "cpu_ms": round(self.cpu_ms, 2),
            # >100% = used more than one core; ~0% = blocked, not working.
            "cpu_pct": (round(self.cpu_ms / self.ms * 100.0, 1) if self.ms > 0 else None),
            "thread_cpu_ms": round(self.thread_cpu_ms, 2),
            # THE memory number: peak inside this stage minus RSS at entry.
            "rss_peak_delta_mb": _debug_mb(self.rss_peak_delta_b),
            "rss_peak_mb": _debug_mb(self.rss_peak_b),
            # What it KEPT — legitimately negative when pages go back to the OS.
            "rss_end_delta_mb": _debug_mb(self.rss_end_delta_b),
            "rss_after_mb": _debug_mb(self.rss_after_b),
        }
        if self.calls != 1:
            d["calls"] = self.calls
            d["ms_max"] = round(self.ms_max, 2)
        if kids:
            d["children"] = [k.to_dict() for k in kids]
        return d


class _StageProfiler:
    """SPEAKER-DEBUG: nested per-stage latency / CPU / memory. Never raises."""

    # RSS is sampled on a background thread because endpoint-only sampling
    # misses any allocation freed before the stage exits — see the block
    # comment above for the traces that exposed it.
    _SAMPLE_INTERVAL_S = 0.02
    _MAX_SAMPLES = 20000        # ~400 s of sampling; backstop, not a real limit
    _MAX_LIFETIME_S = 300.0     # sampler self-terminates if a call never ends

    def __init__(self, label: str) -> None:
        self.label = label
        self._t0 = time.perf_counter()
        self._cpu0 = time.process_time()
        self._rss0 = _debug_rss_bytes()
        self._root = _StageNode(label)
        # Open-stage stack: a stage entered while another is open becomes its
        # child. This is what makes the output a tree instead of a flat list.
        self._stack: list[_StageNode] = [self._root]
        self._samples: list[tuple[float, int]] = []
        self._stop = threading.Event()
        if self._rss0 is not None:
            self._samples.append((self._t0, self._rss0))
            # Daemon: must never hold up interpreter shutdown. Self-terminates
            # on _MAX_LIFETIME_S so a call that dies before to_dict() can't
            # leak a sampler for the life of the process.
            threading.Thread(
                target=self._sample_loop,
                name="speaker-debug-rss",
                daemon=True,
            ).start()

    def _sample_loop(self) -> None:
        deadline = self._t0 + self._MAX_LIFETIME_S
        try:
            while not self._stop.wait(self._SAMPLE_INTERVAL_S):
                if (
                    len(self._samples) >= self._MAX_SAMPLES
                    or time.perf_counter() > deadline
                ):
                    return
                rss = _debug_rss_bytes()
                if rss is not None:
                    self._samples.append((time.perf_counter(), rss))
        except Exception:
            pass

    def close(self) -> None:
        """Stop the sampler. Idempotent."""
        self._stop.set()

    def _peak_between(
        self, t0: float, t1: float, *points: Optional[int]
    ) -> Optional[int]:
        """Highest RSS observed in [t0, t1], including the endpoint reads."""
        best: Optional[int] = None
        for p in points:
            if p is not None and (best is None or p > best):
                best = p
        for t, rss in list(self._samples):  # snapshot; sampler only appends
            if t0 <= t <= t1 and (best is None or rss > best):
                best = rss
        return best

    @contextmanager
    def stage(self, name: str):
        """Time + measure one stage. Records even when the body raises."""
        node = self._stack[-1].child(name)
        self._stack.append(node)
        t0 = time.perf_counter()
        cpu0 = time.process_time()
        thread0 = time.thread_time()
        rss_before = _debug_rss_bytes()
        try:
            yield
        finally:
            # `finally`, so a stage that REJECTS (VAD trims to nothing, STOI
            # below threshold) still reports its cost — a reject pays for the
            # same inference as a pass, and is exactly what we tune.
            t1 = time.perf_counter()
            cpu_ms = (time.process_time() - cpu0) * 1000.0
            thread_ms = (time.thread_time() - thread0) * 1000.0
            rss_after = _debug_rss_bytes()
            try:
                if self._stack and self._stack[-1] is node:
                    self._stack.pop()
                node.accumulate(
                    (t1 - t0) * 1000.0, cpu_ms, thread_ms,
                    rss_before, rss_after,
                    self._peak_between(t0, t1, rss_before, rss_after),
                )
            except Exception:  # a profiler must never break the service
                pass

    def to_dict(self) -> Optional[dict[str, Any]]:
        """JSON-safe profile for the trace's profile.json."""
        try:
            self.close()
            t1 = time.perf_counter()
            rss_end = _debug_rss_bytes()
            peak = self._peak_between(self._t0, t1, self._rss0, rss_end)
            total_ms = (t1 - self._t0) * 1000.0
            cpu_ms = (time.process_time() - self._cpu0) * 1000.0
            return {
                "total_ms": round(total_ms, 2),
                "cpu_ms": round(cpu_ms, 2),
                "cpu_pct": (round(cpu_ms / total_ms * 100.0, 1) if total_ms > 0 else None),
                "cpu_count": os.cpu_count(),
                "rss_source": _debug_rss_mode,
                "rss_sample_interval_ms": round(self._SAMPLE_INTERVAL_S * 1000.0, 1),
                "rss_samples": len(self._samples),
                "rss_start_mb": _debug_mb(self._rss0),
                "rss_end_mb": _debug_mb(rss_end),
                "rss_peak_mb": _debug_mb(peak),
                "rss_peak_delta_mb": _debug_mb(
                    None if (peak is None or self._rss0 is None) else peak - self._rss0
                ),
                "rss_end_delta_mb": _debug_mb(
                    None if (rss_end is None or self._rss0 is None)
                    else rss_end - self._rss0
                ),
                # Process high-water mark since start-up (Linux VmHWM) — a
                # whole-process number, unrelated to this call's own peak.
                "process_peak_rss_mb": _debug_mb(_debug_peak_rss_bytes()),
                "stages": [k.to_dict() for k in self._root.kids()],
            }
        except Exception as e:
            logger.debug("SPEAKER-DEBUG profile serialize failed: %s", e)
            return None

    def summary_line(self) -> str:
        """One-line `path=<ms>/<cpu%>/<+peak MB>` summary for the log."""
        try:
            parts = [f"total={(time.perf_counter() - self._t0) * 1000.0:.1f}ms"]

            def walk(node: _StageNode, prefix: str) -> None:
                for k in node.kids():
                    path = f"{prefix}{k.name}"
                    seg = f"{path}{'' if k.calls == 1 else f'x{k.calls}'}={k.ms:.1f}ms"
                    if k.ms > 0:
                        seg += f"/{k.cpu_ms / k.ms * 100.0:.0f}%cpu"
                    peak = _debug_mb(k.rss_peak_delta_b)
                    if peak is not None:
                        seg += f"/+{peak:.1f}MB"
                    parts.append(seg)
                    walk(k, path + ".")

            walk(self._root, "")
            return " ".join(parts)
        except Exception:
            return "<unavailable>"


class _SpeakerDebugTracer:
    """SPEAKER-DEBUG: writes per-call trace dirs. Fully self-contained; never raises."""

    def __init__(self) -> None:
        # SPEAKER-DEBUG: OFF by default (production-safe). Set HAL_SPEAKER_DEBUG=true
        # to enable during development — no .env edit needed, any env source works
        # (shell `export`, systemd `Environment=`, docker `-e`, the launch script).
        # Read once at construction, so restart HAL after changing it.
        self.enabled = os.environ.get(
            "HAL_SPEAKER_DEBUG", "false").lower() == "true"
        # Default: a `speaker_logs/` dir right next to this file, so traces are
        # trivial to inspect. Override with HAL_SPEAKER_DEBUG_DIR.
        _default_dir = Path(__file__).resolve().parent / "speaker_logs"
        self._base = Path(os.environ.get("HAL_SPEAKER_DEBUG_DIR", str(_default_dir)))
        try:
            self._max = int(os.environ.get("HAL_SPEAKER_DEBUG_MAX_ENTRIES", "1000"))
        except ValueError:
            self._max = 1000
        if self.enabled:
            # Prefer the source-tree dir; if it's read-only (device deploy),
            # fall back to a writable temp dir instead of silently disabling.
            if not self._try_mkdir(self._base):
                import tempfile
                fallback = Path(tempfile.gettempdir()) / "hal-speaker-debug"
                if self._try_mkdir(fallback):
                    logger.warning(
                        "SPEAKER-DEBUG: %s not writable — using %s",
                        self._base, fallback,
                    )
                    self._base = fallback
                else:
                    logger.warning("SPEAKER-DEBUG disabled (no writable dir)")
                    self.enabled = False
            if self.enabled:
                logger.info("SPEAKER-DEBUG tracing ON -> %s", self._base)

    @staticmethod
    def _try_mkdir(p: Path) -> bool:
        try:
            p.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False

    @staticmethod
    def _stamp() -> str:
        now = time.time()
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int((now % 1.0) * 1e6):06d}"

    @staticmethod
    def _san(value: Any) -> str:
        s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")
        return s[:48] or "na"

    def record(
        self,
        kind: str,
        *,
        cls: Any = None,
        confidence: Optional[float] = None,
        reason: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        wavs: Optional[dict[str, bytes]] = None,
        arrays: Optional[dict[str, Any]] = None,
        profile: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            stamp = self._stamp()
            if reason:
                dname = f"{stamp}_FAIL-{self._san(reason)}"
            else:
                conf = f"{float(confidence):.2f}" if confidence is not None else "na"
                dname = f"{stamp}_{self._san(cls)}_{conf}"
            out = self._base / kind / dname
            out.mkdir(parents=True, exist_ok=True)

            payload: dict[str, Any] = {
                "timestamp": stamp,
                "ts": time.time(),
                "service": f"speaker.{kind}",
                "status": "failure" if reason else "prediction",
            }
            if reason:
                payload["reason"] = reason
            if result:
                payload.update(result)
            (out / "result.json").write_text(json.dumps(payload, indent=2, default=str))

            # Latency/memory goes in its OWN file — result.json is already dense
            # with the recognition decision, and mixing timings into it makes
            # both harder to read. Same dir, so a trace stays one unit.
            if profile:
                (out / "profile.json").write_text(
                    json.dumps(profile, indent=2, default=str)
                )

            for fn, wb in (wavs or {}).items():
                if wb:
                    try:
                        (out / fn).write_bytes(wb)
                    except OSError:
                        pass
            for fn, arr in (arrays or {}).items():
                if arr is not None:
                    try:
                        np.save(out / fn, np.asarray(arr))
                    except Exception:
                        pass
            self._prune(kind)
        except Exception as e:  # a debug tracer must never break the service
            logger.debug("SPEAKER-DEBUG trace failed: %s", e)

    def _prune(self, kind: str) -> None:
        if self._max <= 0:
            return
        try:
            kd = self._base / kind
            dirs = sorted((p for p in kd.iterdir() if p.is_dir()), key=lambda p: p.name)
            for old in dirs[: max(0, len(dirs) - self._max)]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass

    @staticmethod
    def classify_reason(err: Exception) -> str:
        """SPEAKER-DEBUG: map a recognize/enroll exception to a short FAIL slug."""
        if isinstance(err, EmbeddingAPIUnavailableError):
            return "server-error"
        msg = str(err).lower()
        # On-device preprocessing gate rejections (PreprocessRejected reason
        # codes are embedded in the message as "[<reason>]").
        if "empty_input" in msg:
            return "empty-input"
        if "vad_removed_all" in msg:
            return "no-voice"
        if "low_voice_ratio" in msg:
            return "low-voice"
        if "low_intelligibility" in msg:
            return "low-stoi"
        if "too_short" in msg or "too short" in msg:
            return "too-short"
        if "too silent" in msg:
            return "too-silent"
        if "not configured" in msg:
            return "api-not-configured"
        if "invalid base64" in msg or "cannot decode" in msg or "empty audio" in msg:
            return "bad-audio"
        return "embed-error"
# ===================  end SPEAKER-DEBUG tracer block  =====================


class SpeakerRecognizer:
    """Per-user voice embedding store with external-API embedding computation."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        users_dir: Optional[Path] = None,
        match_threshold: Optional[float] = None,
    ) -> None:
        self._api_url = api_url or _API_URL
        self._api_key = api_key or _API_KEY
        # recognize() runs per utterance — without this, a BYO LLM host with
        # no dl_base_url configured logs "not configured" on every single
        # utterance instead of once.
        self._logged_unavailable = False
        self._users_dir = Path(users_dir) if users_dir else _USERS_DIR
        self._match_threshold = (
            match_threshold if match_threshold is not None else _MATCH_COS
        )
        self._mu = threading.Lock()

        # --- Embedding-model identity tracking ------------------------------
        # Every stored embedding is only comparable to a query embedding from
        # the SAME server model. We stamp each enrollment with the server's
        # model version and re-embed (migrate) stored WAVs when the server model
        # changes. `_server_model_version` is refreshed as a side effect of every
        # /embed call (see _call_embedding_api). Migration runs in a single
        # background thread guarded by these fields.
        self._server_model_version: Optional[str] = None
        self._migration_lock = threading.Lock()
        self._migrating_version: Optional[str] = None
        # Per-user locks serialize the on-disk commit (samples + sidecars +
        # metadata.json) between enroll() and the migration re-embed, so the two
        # never interleave writes to the same profile. Created lazily under _mu.
        self._user_locks: dict[str, threading.Lock] = {}

        # Bank cache + the lock guarding it and every extended-set mutation.
        # RLock because the extend path takes it for two short critical sections
        # around an unlocked disk write, and prune runs nested inside the
        # second. Disk I/O must NEVER happen while this is held — see
        # _maybe_extend_user, and faceid/recognizer.py:282 for what went wrong
        # in the face pipeline when it did.
        self._bank_lock = threading.RLock()
        self._bank_cache: Optional[
            tuple[Optional[np.ndarray], list[str], list[str]]
        ] = None
        self._bank_cache_sig: Optional[tuple] = None
        # Monotonic counter appended to extended filenames so two samples
        # captured in the same millisecond cannot overwrite each other.
        self._extended_seq: int = 0

        # Live count of incoming_*.wav in the log root, so a turn knows whether
        # it is over the cap without stat-ing the directory. Seeded from disk,
        # which is what trims a device that ran before the cap existed: an old
        # backlog is counted at startup and rolled away on the first turn.
        self._roll_lock = threading.Lock()
        self._incoming_count: int = self._count_incoming()

        self._debug = _SpeakerDebugTracer()  # SPEAKER-DEBUG (remove before deploy)
        # SPEAKER-DEBUG: last stranger-cluster match info, stashed by
        # _assign_voiceprint_hash so the recognize() trace can record the
        # cluster match score / re-appearance without changing that method's
        # return signature. Only ever written when self._debug.enabled.
        self._debug_stranger: Optional[dict[str, Any]] = None
        # SPEAKER-DEBUG: last on-device preprocessing snapshot (cleaned WAV +
        # VAD/STOI metrics), stashed by _prepare_wav_for_embedding so the
        # recognize() trace can log what the gate produced. Reset per recognize.
        self._debug_preproc: Optional[dict[str, Any]] = None
        # SPEAKER-DEBUG: the PARTIAL chain output when a stage rejects — the
        # audio the last successful stage produced. Without this a STOI reject
        # traced only input.wav, so there was no way to hear what TEN-VAD
        # actually handed the gate, which is the audio the reject is about.
        # Reset at the top of every _prepare_wav_for_embedding.
        self._debug_partial: Optional[dict[str, Any]] = None
        # SPEAKER-DEBUG: per-call stage profiler (latency + RSS per stage).
        # THREAD-LOCAL, unlike the two snapshots above: recognize()/enroll() are
        # not serialized against each other, and a profile is an accumulating
        # list — two concurrent calls sharing one would interleave their stages
        # and report nonsense timings.
        self._debug_prof = threading.local()

        self._crypto: CryptoSession | None = None
        if config.DL_ENCRYPTION_ENABLED:
            public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
            if public_key is not None:
                self._crypto = CryptoSession(public_key)
                logger.info("Speaker recognizer: encryption enabled")
            elif config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")

        self._users_dir.mkdir(parents=True, exist_ok=True)
        _UNKNOWN_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

        # Voice stranger clustering state — mirrors FaceRecognizer stranger
        # tracking. Persists to _VOICE_STRANGERS_DIR so reboots don't lose
        # the "same voice seen again" grouping.
        self._stranger_lock = threading.Lock()
        self._stranger_embeds: Optional[np.ndarray] = None  # [N, D] L2-normalized
        self._stranger_labels: Optional[np.ndarray] = None  # [N] str labels
        self._stranger_counter: int = 0
        self._stranger_model_version: Optional[str] = None
        _VOICE_STRANGERS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_strangers()
        # Clear cluster dirs left orphaned by the previous row-based eviction,
        # which dropped centroids without ever touching disk.
        self._reconcile_cluster_dirs()

        logger.info(
            "SpeakerRecognizer ready (api=%s, threshold=%.2f, users_dir=%s, strangers=%d)",
            self._api_url or "<unset>",
            self._match_threshold,
            self._users_dir,
            0 if self._stranger_embeds is None else len(self._stranger_embeds),
        )

        # On restart, proactively check whether the server model changed while we
        # were down and re-embed any stale profiles.
        if self.available:
            self._spawn_startup_reconcile()

    @property
    def available(self) -> bool:
        return bool(self._api_url)

    # ------------------------------------------------------------------ paths

    def _voice_dir(self, norm: str) -> Path:
        return self._users_dir / norm / _VOICE_SUBDIR

    def _extended_dir(self, norm: str) -> Path:
        return self._voice_dir(norm) / _EXTENDED_SUBDIR

    def _embedding_path(self, norm: str) -> Path:
        """LEGACY aggregated-vector path. Read-only — nothing writes this now."""
        return self._voice_dir(norm) / _EMBEDDING_FILE

    def _has_profile(self, norm: str) -> bool:
        """Whether this user has any usable voice bank on disk.

        Replaces the old ``_embedding_path(norm).is_file()`` check: enrollments
        made after the bank rewrite have no ``embedding.npy`` at all, so that
        test would report every new user as un-enrolled.
        """
        voice_dir = self._voice_dir(norm)
        if not voice_dir.is_dir():
            return False
        if self._embedding_path(norm).is_file():
            return True  # legacy one-row profile
        return any(
            _sidecar_path(p).is_file() for p in voice_dir.glob("sample_*.wav")
        )

    def _metadata_path(self, norm: str) -> Path:
        return self._voice_dir(norm) / _METADATA_FILE

    # ------------------------------------------------------------- registry

    def _load_registry(self) -> dict[str, Any]:
        if _REGISTRY_FILE.is_file():
            try:
                return json.loads(_REGISTRY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_registry(self, registry: dict[str, Any]) -> None:
        try:
            _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _REGISTRY_FILE.with_name(_REGISTRY_FILE.name + ".tmp")
            tmp.write_text(json.dumps(registry, indent=2))
            os.replace(tmp, _REGISTRY_FILE)
        except OSError as e:
            logger.warning("failed to save voice registry: %s", e)

    def _update_registry(self, norm: str, meta: dict[str, Any]) -> None:
        with self._mu:
            reg = self._load_registry()
            reg[norm] = {
                "display_name": meta.get("display_name", norm),
                "telegram_username": meta.get("telegram_username", ""),
                "telegram_id": meta.get("telegram_id", ""),
                "has_telegram_identity": meta.get("has_telegram_identity", False),
                "enrollment_sources": meta.get("enrollment_sources", []),
                "last_enrollment_source": meta.get("last_enrollment_source", ""),
                "enrolled_at": meta.get("enrolled_at"),
                "updated_at": meta.get("updated_at"),
                "num_samples": meta.get("num_samples", 0),
                "num_extended": meta.get("num_extended", 0),
                "embedding_dim": meta.get("embedding_dim", 0),
                "embed_model_version": meta.get("embed_model_version", ""),
            }
            self._save_registry(reg)

    def _remove_from_registry(self, norm: str) -> None:
        with self._mu:
            reg = self._load_registry()
            if norm in reg:
                del reg[norm]
                self._save_registry(reg)

    def _user_lock(self, norm: str) -> threading.Lock:
        """Per-user commit lock (created on first use). Held only briefly under
        ``_mu`` to fetch/create it, so callers never hold ``_mu`` while waiting on
        the returned lock — the lock order is always user-lock → ``_mu``."""
        with self._mu:
            lock = self._user_locks.get(norm)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[norm] = lock
            return lock

    # -------------------------------------------------------------- external

    def _call_embedding_api(
        self, audios_b64: list[str], *, use_sliding_window: bool = False
    ) -> np.ndarray:
        """POST audios to the embedding API.

        ``use_sliding_window=False`` (default, enroll path): the server embeds
        the WHOLE utterance as a single chunk — no windowing/mean — and this
        returns the L2-normalized aggregated vector ``[D]``.

        ``use_sliding_window=True`` (recognize path): the server slides
        overlapping windows and this returns the matrix of per-chunk embeddings
        ``[M, D]`` for per-chunk voting against stored speakers (mirroring
        perception-service's /recognize logic).
        """
        if not self._api_url:
            raise SpeakerRecognizerError(
                "SPEAKER_EMBEDDING_API_URL not configured"
            )
        if not audios_b64:
            raise SpeakerRecognizerError("no audio to embed")

        logger.info(
            "Calling embedding API with %d audios at %s (use_sliding_window=%s)",
            len(audios_b64), self._api_url, use_sliding_window,
        )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        body: dict[str, Any] = {
            "audios_b64": audios_b64,
            "preprocess": False,
            "use_sliding_window": use_sliding_window,
        }

        # SPEAKER-DEBUG: `embed_api` = the whole call, `embed_api.request` = the
        # network round-trip alone, `embed_api.decode` = parse + L2 normalize.
        # Both record on failure too (the `finally` in _StageProfiler.stage), so
        # a timeout shows up as its full _API_TIMEOUT_S wait rather than vanishing.
        with self._debug_stage("embed_api"):
            try:
                with self._debug_stage("request"):
                    if self._crypto is not None:
                        resp = requests.post(
                            self._api_url,
                            data=self._crypto.wrap_http_request(json.dumps(body).encode()),
                            headers=headers,
                            timeout=_API_TIMEOUT_S,
                        )
                    else:
                        resp = requests.post(
                            self._api_url,
                            json=body,
                            headers=headers,
                            timeout=_API_TIMEOUT_S,
                        )
            except requests.RequestException as e:
                logger.warning("Embedding server unreachable at %s: %s", self._api_url, e)
                raise EmbeddingAPIUnavailableError(
                    f"embedding API unreachable: {e}"
                ) from e

            if resp.status_code != 200:
                logger.warning(
                    "Embedding server returned HTTP %d: %s",
                    resp.status_code, resp.text[:120],
                )
                # 5xx = server broken → transient, caller should retry.
                # 4xx = server rejected THIS audio (VAD, decode, etc.) → audio-level,
                # caller should skip this sample / re-record.
                if resp.status_code >= 500:
                    raise EmbeddingAPIUnavailableError(
                        f"embedding API {resp.status_code}: {resp.text[:200]}"
                    )
                raise SpeakerRecognizerError(
                    f"embedding API error {resp.status_code}: {resp.text[:200]}"
                )

            with self._debug_stage("decode"):
                try:
                    if self._crypto is not None:
                        payload = json.loads(self._crypto.unwrap_http_response(resp.content))
                    else:
                        payload = resp.json()
                except ValueError as e:
                    raise EmbeddingAPIUnavailableError(
                        f"embedding API returned non-JSON: {e}"
                    ) from e

                server_ver = payload.get("embed_model_version")
                if server_ver:
                    self._server_model_version = str(server_ver)

                if use_sliding_window:
                    chunks = payload.get("chunk_embeddings")
                    if not chunks:
                        raise EmbeddingAPIUnavailableError(
                            "embedding API response missing 'chunk_embeddings'"
                        )
                    mat = np.asarray(chunks, dtype=np.float32)
                    if mat.ndim != 2 or mat.size == 0:
                        raise EmbeddingAPIUnavailableError(
                            f"chunk_embeddings must be a non-empty 2-D array, got shape {mat.shape}"
                        )
                    norms = np.linalg.norm(mat, axis=1, keepdims=True)
                    norms[norms < 1e-12] = 1.0
                    return (mat / norms).astype(np.float32)

                emb = payload.get("embedding")
                if emb is None:
                    raise EmbeddingAPIUnavailableError(
                        "embedding API response missing 'embedding'"
                    )

                vec = np.asarray(emb, dtype=np.float32)
                if vec.ndim != 1 or vec.size == 0:
                    raise EmbeddingAPIUnavailableError(
                        f"embedding must be a non-empty 1-D array, got shape {vec.shape}"
                    )

                norm = float(np.linalg.norm(vec))
                if norm == 0.0:
                    raise EmbeddingAPIUnavailableError("embedding has zero norm")
                return vec / norm

    def _prepare_wav_for_embedding(self, wav_bytes: bytes) -> list[str]:
        """Run the on-device preprocessing pipeline and wrap the cleaned WAV.

        HAL now runs the full filter/VAD/normalize chain locally (Mono →
        Resample → [HighPass] → [NoiseReduce] → VAD → [STOI] → RMS) — the
        pipeline that used to run inside perception-service, plus the HAL-only
        STOI intelligibility gate. Only audio that PASSES the gate
        is returned for upload; ``_call_embedding_api`` then asks the server to
        skip its own preprocessing (``preprocess=false``) and just compute the
        embedding on this cleaned waveform.

        The ``/embed`` endpoint still windows/chunks the waveform itself, so we
        pass the whole cleaned WAV as a single element and let the server slice
        it (client-side splitting would only add a lossy float32 → PCM_16
        round-trip per slice).

        Raises ``SpeakerRecognizerError`` (audio-level) when the pipeline
        rejects the clip — recognize() maps that to "unknown" and enroll()
        skips the sample, exactly as when the server returned HTTP 400. Note
        this is deliberately NOT ``EmbeddingAPIUnavailableError``: a rejected
        clip is a bad recording, not a server outage.
        """
        # Light submodule imports (Audio / PreprocessRejected) avoid pulling in
        # onnxruntime at module import time — that loads in _get_audio_processor.
        from hal.drivers.voice.speaker_recognizer.audio_processors.base import Audio
        from hal.drivers.voice.speaker_recognizer.audio_processors.exceptions import (
            PreprocessRejected,
        )

        # SPEAKER-DEBUG: `prof` is None unless tracing is on; every stage below
        # is wrapped so the whole chain (and each gate inside it) is profiled.
        prof: Optional[_StageProfiler] = getattr(self._debug_prof, "cur", None)
        self._debug_partial = None  # SPEAKER-DEBUG: never report a stale partial
        with self._debug_stage("preprocess"):
            with self._debug_stage("decode_wav"):
                waveform = _wav_bytes_to_float32_16k_mono(wav_bytes)
            if waveform.shape[0] == 0:
                raise SpeakerRecognizerError("empty audio for embedding")

            # First call also loads the TEN-VAD + ONNX STOI sessions, which is
            # the single largest memory step in the pipeline — worth its own
            # stage so a cold call isn't mistaken for a per-clip cost.
            with self._debug_stage("processor_init"):
                processor = _get_audio_processor()
            try:
                audio_in = Audio(waveform=waveform, sample_rate=_TARGET_SR)
                if prof is not None:  # SPEAKER-DEBUG: per-stage walk of the chain
                    cleaned = self._debug_profiled_process(processor, audio_in, prof)
                else:
                    cleaned = processor.process(audio_in)
            except PreprocessRejected as e:
                # Audio-level rejection (a bad recording), NOT a server outage —
                # map to SpeakerRecognizerError so recognize() reads "unknown" and
                # every batch caller (enroll, migration) SKIPS this clip without
                # deleting any previously-accepted on-disk sample. The gate is a
                # moving target; a stored WAV is not "corrupt" merely because
                # today's VAD/STOI trims it below threshold.
                err = SpeakerRecognizerError(
                    f"audio rejected by preprocessing gate [{e.reason}]: {e}"
                )
                err.gate_detail = e.to_dict()  # SPEAKER-DEBUG: reason + VAD/STOI metrics
                raise err from e

            out = np.asarray(cleaned.waveform, dtype=np.float32)
            if out.shape[0] == 0:
                raise SpeakerRecognizerError("preprocessing produced empty audio")

            with self._debug_stage("encode_wav"):
                cleaned_wav = _float32_waveform_to_wav_bytes(out)
                payload = [base64.b64encode(cleaned_wav).decode("ascii")]
            if self._debug.enabled:  # SPEAKER-DEBUG
                self._debug_preproc = self._debug_preproc_snapshot(
                    out, cleaned_wav, processor
                )
        return payload

    def _debug_preproc_snapshot(  # SPEAKER-DEBUG (remove before deploy)
        self, cleaned: np.ndarray, cleaned_wav: bytes, processor: Any
    ) -> dict[str, Any]:
        """Snapshot the on-device preprocessing result for the recognize trace.

        Captures the cleaned/uploaded WAV plus its duration/RMS and the STOI
        gate's pass score (pulled off the SpeechIntelligibilityFilter stage, if
        present). Only called when debug is enabled.
        """
        dur = round(float(cleaned.shape[0]) / _TARGET_SR, 3)
        rms = (round(float(np.sqrt(np.mean(cleaned.astype(np.float64) ** 2))), 6)
               if cleaned.size else 0.0)
        stoi_score: Optional[float] = None
        stoi_threshold: Optional[float] = None
        for p in getattr(processor, "_processors", []):
            if type(p).__name__ == "SpeechIntelligibilityFilter":
                s = getattr(p, "last_score", float("nan"))
                stoi_score = None if (s is None or np.isnan(s)) else round(float(s), 4)
                stoi_threshold = getattr(p, "_threshold", None)
                break
        return {
            "cleaned_wav": cleaned_wav,          # popped into the trace's wavs
            "cleaned_duration_s": dur,
            "cleaned_rms": rms,
            "stoi_score": stoi_score,
            "stoi_threshold": stoi_threshold,
        }

    def _debug_preproc_parts(self) -> tuple[dict[str, bytes], Optional[dict[str, Any]]]:
        """SPEAKER-DEBUG: split the last preprocessing snapshot into
        (wav-attachments, json-safe metrics) for a recognize trace."""
        pp = self._debug_preproc or {}
        wavs: dict[str, bytes] = {}
        if pp.get("cleaned_wav"):
            wavs["preprocessed.wav"] = pp["cleaned_wav"]
        metrics = {k: v for k, v in pp.items() if k != "cleaned_wav"} or None
        return wavs, metrics

    # ------------------------------------ SPEAKER-DEBUG: stage profiling

    def _debug_profile_start(self, label: str) -> None:
        """SPEAKER-DEBUG: begin a per-call profile. No-op when tracing is off."""
        self._debug_prof.cur = _StageProfiler(label) if self._debug.enabled else None

    def _debug_stage(self, name: str) -> Any:
        """SPEAKER-DEBUG: context manager timing one stage; no-op when off.

        Used at every call site so the production path costs one attribute
        lookup and a ``nullcontext``.
        """
        prof = getattr(self._debug_prof, "cur", None)
        return prof.stage(name) if prof is not None else nullcontext()

    def _debug_profile_dict(self) -> Optional[dict[str, Any]]:
        """SPEAKER-DEBUG: finish the profile — log the summary, return the JSON.

        Passed as ``record(profile=...)`` at each trace point, which writes it
        to ``profile.json`` beside result.json in the same per-call dir.
        """
        prof = getattr(self._debug_prof, "cur", None)
        if prof is None:
            return None
        logger.info(
            "SPEAKER-DEBUG profile [%s]: %s", prof.label, prof.summary_line()
        )
        return prof.to_dict()

    def _debug_profiled_process(
        self, processor: Any, audio: Any, prof: _StageProfiler
    ) -> Any:
        """SPEAKER-DEBUG: run the preprocessing chain stage-by-stage.

        Mirrors ``CompositeAudioProcessor._process_impl`` exactly — same order,
        same ``processor.process(...)`` per stage, same exceptions — and only
        adds a timer around each one, so TEN-VAD and the STOI gate each get
        their own latency + memory numbers instead of one opaque "preprocess"
        total. Kept HERE rather than inside ``audio_processors/`` so the whole
        SPEAKER-DEBUG block stays removable without touching that package.
        Falls back to the plain composite call if the chain isn't introspectable.

        Also records the PARTIAL result when a stage rejects: the audio the last
        successful stage produced is stashed on ``self._debug_partial`` before
        the exception propagates, so a STOI reject can still dump the TEN-VAD
        output the gate said no to.
        """
        stages = getattr(processor, "_processors", None)
        if not stages:
            with prof.stage("chain"):
                return processor.process(audio)
        result = audio
        last_ok: Optional[str] = None  # SPEAKER-DEBUG: last stage that returned
        for p in stages:
            cls_name = type(p).__name__
            stage_name = _DEBUG_STAGE_NAMES.get(cls_name, cls_name.lower())
            try:
                # Leaf name only — the profiler nests it under whatever stage is
                # open (``preprocess``), so the tree carries the path.
                with prof.stage(stage_name):
                    result = p.process(result)
            except Exception:
                # SPEAKER-DEBUG: `result` still holds the last successful
                # stage's output. Capture it, then re-raise untouched — this
                # must not change which exception the caller sees.
                self._debug_capture_partial(result, last_ok, stage_name)
                raise
            last_ok = stage_name
        return result

    def _debug_capture_partial(  # SPEAKER-DEBUG (remove before deploy)
        self, partial: Any, after_stage: Optional[str], rejected_by: str
    ) -> None:
        """Stash the chain output as it stood when ``rejected_by`` refused it.

        ``after_stage`` is the last stage that actually ran (None if the very
        first one rejected, in which case there is nothing to dump that
        ``input.wav`` doesn't already show). Never raises — a debug aid must not
        turn a gate rejection into a crash.
        """
        try:
            if after_stage is None:
                return
            wf = np.asarray(getattr(partial, "waveform", None), dtype=np.float32)
            if wf.ndim != 1 or wf.shape[0] == 0:
                return
            sr = int(getattr(partial, "sample_rate", _TARGET_SR))
            self._debug_partial = {
                # Named for the stage that produced it, so the file says what it
                # is: `after_ten_vad.wav` next to a FAIL-low-stoi result.json.
                "wav_name": f"after_{after_stage}.wav",
                "wav": _float32_waveform_to_wav_bytes(wf, sr),
                "after_stage": after_stage,
                "rejected_by": rejected_by,
                "duration_s": round(float(wf.shape[0]) / float(max(1, sr)), 3),
                "rms": round(float(np.sqrt(np.mean(wf.astype(np.float64) ** 2))), 6),
                "sample_rate": sr,
            }
        except Exception as e:
            logger.debug("SPEAKER-DEBUG partial-chain capture failed: %s", e)

    def _debug_partial_parts(self) -> tuple[dict[str, bytes], Optional[dict[str, Any]]]:
        """SPEAKER-DEBUG: split the partial-chain snapshot into
        (wav-attachments, json-safe metrics) for a FAIL trace."""
        pp = self._debug_partial or {}
        wavs: dict[str, bytes] = {}
        if pp.get("wav") and pp.get("wav_name"):
            wavs[str(pp["wav_name"])] = pp["wav"]
        metrics = {k: v for k, v in pp.items() if k != "wav"} or None
        return wavs, metrics

    # ------------------------------------------------------------- metadata

    def _read_metadata(self, norm: str) -> dict[str, Any]:
        p = self._metadata_path(norm)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _stored_model_version(self, norm: str) -> str:
        """Embed-model version a profile was last computed under ("" if none)."""
        return self._read_metadata(norm).get("embed_model_version") or ""

    def _read_shared_metadata(self, norm: str) -> dict[str, Any]:
        """Read the top-level ``/root/local/users/<norm>/metadata.json``.

        Shared with FaceRecognizer — source of truth for telegram_* fields.
        """
        p = self._users_dir / norm / "metadata.json"
        if p.is_file():
            try:
                return json.loads(p.read_text()) or {}
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _write_metadata(self, norm: str, meta: dict[str, Any]) -> None:
        self._metadata_path(norm).write_text(json.dumps(meta, indent=2))

    @staticmethod
    def _now_iso() -> str:
        """ISO-8601 timestamp with tz offset (naive fallback if unavailable)."""
        return time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

    # ------------------------------------------------------------- bank (disk)

    @staticmethod
    def _read_sidecar(wav_path: Path) -> Optional[np.ndarray]:
        """L2-normalized embedding stored beside ``wav_path``, or None.

        Trusted as-is — deliberately NOT re-gated against the preprocessing
        chain. A sample worth keeping (far mic, quiet delivery) is exactly the
        one a stricter gate would now reject, and re-running inference on every
        load would also mean a remote API call per sample per restart.
        """
        p = _sidecar_path(wav_path)
        if not p.is_file():
            return None
        try:
            vec = np.load(p).astype(np.float32).reshape(-1)
        except (OSError, ValueError) as e:
            logger.warning("bad sidecar %s: %s", p, e)
            return None
        if vec.size == 0 or float(np.linalg.norm(vec)) < 1e-12:
            return None
        return _l2(vec)

    @staticmethod
    def _write_sidecar(wav_path: Path, embedding: np.ndarray) -> None:
        """Atomically (over)write the embedding sidecar beside a sample WAV.

        temp + os.replace so a concurrent bank read never sees a torn file and a
        crash mid-migration leaves whole sidecars, never a corrupt one. Passing a
        file object to np.save stops it appending a second .npy to the temp name.
        """
        p = _sidecar_path(wav_path)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "wb") as f:
            np.save(f, _l2(embedding))
        os.replace(tmp, p)

    def _read_tier(self, wav_paths: list[Path]) -> tuple[list[np.ndarray], list[Path]]:
        """Sidecar embeddings for a list of sample WAVs, skipping any without one."""
        embs: list[np.ndarray] = []
        keep: list[Path] = []
        for p in wav_paths:
            emb = self._read_sidecar(p)
            if emb is None:
                continue
            embs.append(emb)
            keep.append(p)
        return embs, keep

    def _anchor_wavs(self, norm: str) -> list[Path]:
        return sorted(self._voice_dir(norm).glob("sample_*.wav"))

    def _disk_sample_fields(self, norm: str) -> dict[str, Any]:
        """Sample counts and file lists read LIVE from disk.

        metadata.json still records these at enroll time, but reads derive them
        from the directory instead so they cannot drift. Anything may remove a
        sample between enrolls — the OS server's per-file delete button is the
        common case — and under the old model that drift was hidden because the
        delete path re-ran a full enroll purely to refresh the profile. That
        re-enroll duplicated every surviving sample, so it was removed; deriving
        here is what makes the follow-up call unnecessary rather than merely
        skipped.
        """
        anchor_paths = self._anchor_wavs(norm)
        extended_paths = self._extended_wavs(norm)
        return {
            "num_samples": len(anchor_paths),
            "sample_files": [p.name for p in anchor_paths],
            "sample_origins": {p.name: _sample_origin(p.name) for p in anchor_paths},
            "num_extended": len(extended_paths),
            "extended_files": [p.name for p in extended_paths],
        }

    def _reconcile_sidecars(self, norm: str) -> int:
        """Delete embedding sidecars whose sample WAV is gone.

        The mirror image of the backfill in :meth:`enroll` — that creates a
        missing sidecar for an existing WAV, this removes a sidecar left behind
        by a deleted WAV. Safe in a way that deleting audio never is: a sidecar
        is DERIVED data, so an orphan loses nothing recoverable.

        Orphans are invisible to matching (the bank indexes by WAV) but they
        show up in the voice-file listing, and the OS server correctly refuses
        to let the UI delete a .npy directly — so without this there is no way
        to clear one. Covers devices that accumulated orphans before the OS
        server started removing sidecars alongside their WAV.
        """
        removed = 0
        for d in (self._voice_dir(norm), self._extended_dir(norm)):
            if not d.is_dir():
                continue
            for npy in sorted(d.glob("*.npy")):
                if npy.name == _EMBEDDING_FILE:
                    continue  # legacy aggregate, not a sidecar
                if npy.with_suffix(".wav").is_file():
                    continue
                try:
                    npy.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning("failed to remove orphan sidecar %s: %s", npy, e)
        if removed:
            logger.info("Reconciled %d orphan sidecar(s) for '%s'", removed, norm)
        return removed

    def _extended_wavs(self, norm: str) -> list[Path]:
        ext_dir = self._extended_dir(norm)
        if not ext_dir.is_dir():
            return []
        return sorted(ext_dir.glob(f"{_EXTENDED_PREFIX}*.wav"))

    def _load_user_bank(
        self, norm: str
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Return ``(anchor [Na, D], extended [Ne, D])`` for one user.

        Legacy fallback: a profile enrolled before the bank rewrite has no
        sidecars, only the aggregated ``embedding.npy``. That vector is loaded
        as a one-row anchor bank so the user keeps matching. Nothing rewrites it
        — the bank fills in naturally as they are re-enrolled or auto-extended.
        """
        anchor_embs, _ = self._read_tier(self._anchor_wavs(norm))
        if not anchor_embs:
            legacy = self._embedding_path(norm)
            if legacy.is_file():
                try:
                    vec = np.load(legacy).astype(np.float32).reshape(-1)
                    if vec.size and float(np.linalg.norm(vec)) >= 1e-12:
                        anchor_embs = [_l2(vec)]
                except (OSError, ValueError) as e:
                    logger.warning("failed to load legacy embedding %s: %s", legacy, e)
        ext_embs, _ = self._read_tier(self._extended_wavs(norm))
        anchor = np.stack(anchor_embs, axis=0) if anchor_embs else None
        extended = np.stack(ext_embs, axis=0) if ext_embs else None
        return anchor, extended

    def _bank_signature(self) -> tuple:
        """Cheap invalidation key for the whole-bank cache.

        Sidecars are only ever created or deleted, never edited in place, so a
        directory mtime is a sufficient signal. This exists because recognize()
        loads the bank on EVERY turn and the bank is now N rows per user rather
        than one vector — without a cache that is N times the disk reads on the
        voice hot path.
        """
        sig: list[tuple] = []
        if not self._users_dir.is_dir():
            return ()
        for entry in sorted(self._users_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            voice_dir = entry / _VOICE_SUBDIR
            if not voice_dir.is_dir():
                continue
            try:
                v_mt = voice_dir.stat().st_mtime_ns
            except OSError:
                continue
            ext_dir = voice_dir / _EXTENDED_SUBDIR
            try:
                e_mt = ext_dir.stat().st_mtime_ns if ext_dir.is_dir() else 0
            except OSError:
                e_mt = 0
            sig.append((entry.name, v_mt, e_mt))
        return tuple(sig)

    def _load_bank(self) -> tuple[Optional[np.ndarray], list[str], list[str]]:
        """Load every user's bank, flattened.

        Returns ``(rows [N, D], labels [N], tiers [N])`` where ``tiers[i]`` is
        ``"anchor"`` or ``"extended"``. Flat rather than per-user because
        recognize() scores one matmul against everything, then reduces per
        speaker. Cached against :meth:`_bank_signature`.
        """
        sig = self._bank_signature()
        with self._bank_lock:
            if self._bank_cache is not None and self._bank_cache_sig == sig:
                return self._bank_cache

        rows: list[np.ndarray] = []
        labels: list[str] = []
        tiers: list[str] = []
        if self._users_dir.is_dir():
            for entry in sorted(self._users_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                anchor, extended = self._load_user_bank(entry.name)
                for bank, tier in ((anchor, "anchor"), (extended, "extended")):
                    if bank is None:
                        continue
                    for r in bank:
                        rows.append(r)
                        labels.append(entry.name)
                        tiers.append(tier)

        # Guard against a model change mid-life: rows of differing width cannot
        # be stacked, and silently dropping the minority would be worse than
        # saying so. Keep the most common width and log the rest.
        if rows:
            widths = {int(r.shape[0]) for r in rows}
            if len(widths) > 1:
                keep_dim = max(widths, key=lambda d: sum(1 for r in rows if r.shape[0] == d))
                logger.warning(
                    "Bank holds mixed embedding dims %s — keeping %d, ignoring the rest",
                    sorted(widths), keep_dim,
                )
                filtered = [
                    (r, lb, t)
                    for r, lb, t in zip(rows, labels, tiers)
                    if r.shape[0] == keep_dim
                ]
                rows = [f[0] for f in filtered]
                labels = [f[1] for f in filtered]
                tiers = [f[2] for f in filtered]

        stacked = np.stack(rows, axis=0).astype(np.float32) if rows else None
        result = (stacked, labels, tiers)
        with self._bank_lock:
            self._bank_cache = result
            self._bank_cache_sig = sig
        return result

    def _invalidate_bank(self) -> None:
        """Drop the cached bank after a write that changed it."""
        with self._bank_lock:
            self._bank_cache = None
            self._bank_cache_sig = None

    # -------------------------------------------------- extended tier (disk)

    @staticmethod
    def _delete_sample(wav_path: Path) -> None:
        """Delete a sample WAV and its sidecar (best-effort, never raises)."""
        try:
            wav_path.unlink(missing_ok=True)
            _sidecar_path(wav_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("failed to delete sample %s: %s", wav_path, e)

    def _write_extended_sample(
        self, norm: str, wav_bytes: bytes, embedding: np.ndarray
    ) -> Optional[Path]:
        """Persist one extended sample (WAV + sidecar). Returns its path or None.

        The sidecar is written after the WAV, and a sample only counts as
        present once both exist — a half-written pair is simply invisible to
        the bank loader rather than corrupting it.
        """
        try:
            dest = self._extended_dir(norm)
            dest.mkdir(parents=True, exist_ok=True)
            with self._bank_lock:
                self._extended_seq += 1
                seq = self._extended_seq
            stem = f"{_EXTENDED_PREFIX}{int(time.time() * 1000)}_{seq}"
            wav_path = dest / f"{stem}.wav"
            wav_path.write_bytes(wav_bytes)
            np.save(_sidecar_path(wav_path), _l2(embedding))
            return wav_path
        except OSError as e:
            logger.warning("failed to write extended sample for %s: %s", norm, e)
            return None

    def _prune_extended(self, norm: str) -> list[Path]:
        """Trim a user's extended tier to the most diverse _MAX_EXTENDED_SAMPLES.

        Anchored on the user's enrollment samples, so the kept slots are the
        ones that COMPLEMENT the enrollment rather than repeat it. Returns the
        paths that were deleted. Anchor samples are never candidates here —
        that tier is permanent by design.
        """
        paths = self._extended_wavs(norm)
        embs, kept_paths = self._read_tier(paths)

        # A WAV with no readable sidecar can never be scored or matched, so it
        # is dead weight in this auto-managed tier — drop it. (The anchor tier
        # keeps such files: enrollment audio is the user's, not ours to bin.)
        for p in paths:
            if p not in kept_paths:
                logger.info("Extended prune: dropping unbacked sample %s", p.name)
                self._delete_sample(p)

        if len(embs) <= _MAX_EXTENDED_SAMPLES:
            return []

        # Via _load_user_bank (not _read_tier) so a pre-rewrite profile whose
        # only anchor is the legacy embedding.npy still anchors the selection —
        # reading the tier directly returns nothing for those users and the
        # diversity walk silently falls back to seeding on the newest sample.
        anchor, _ext = self._load_user_bank(norm)
        keep = set(
            _select_diverse(np.stack(embs, axis=0), anchor, _MAX_EXTENDED_SAMPLES)
        )
        dropped = [p for i, p in enumerate(kept_paths) if i not in keep]
        for p in dropped:
            self._delete_sample(p)
        if dropped:
            logger.info(
                "Extended prune '%s': kept %d/%d, dropped %s",
                norm, _MAX_EXTENDED_SAMPLES, len(embs),
                [p.name for p in dropped],
            )
        return dropped

    def _maybe_extend_user(
        self,
        norm: str,
        embedding: np.ndarray,
        wav_bytes: bytes,
        *,
        existing_rows: Optional[np.ndarray],
        duration_s: float,
        margin: float,
    ) -> None:
        """Consider folding one confidently-recognized turn into a user's set.

        Ported from ``faceid/recognizer.py::_maybe_extend_user``, with two
        extra bars that the face pipeline does not need. A camera frame holds
        one face per crop; a turn's audio can hold the TV, a second speaker, or
        the device's own TTS tail — so extending demands more than recognizing:

        * ``duration_s`` must clear _EXTEND_MIN_DURATION_S. A ~1s clip carries
          too little speaker information to be worth a permanent slot, and its
          embedding is noisy enough to look "diverse" for the wrong reason.
        * ``margin`` (winner's confidence minus runner-up's) must clear
          _EXTEND_MIN_MARGIN_COS, so a near-tie between two enrolled users
          never writes audio into either one's bank.

        Then the diversity gate: keep the sample only if its max cosine to what
        we already hold is BELOW _DIVERSITY_COS — above that it duplicates a
        sample we have.

        ``existing_rows`` is this speaker's slice of the bank recognize() just
        matched against — anchor and extended concatenated, legacy fallback
        included, which is exactly what _load_user_bank would rebuild. Taking
        it as an argument rather than re-reading the sidecars saves one np.load
        per stored sample on EVERY recognized turn, almost always to be thrown
        away a line later when the diversity gate rejects a redundant clip.

        Like the face version, this NEVER holds ``_bank_lock`` across disk I/O.
        """
        if duration_s < _EXTEND_MIN_DURATION_S:
            logger.debug(
                "[speaker] extend '%s': skip — %.1fs < %.1fs",
                norm, duration_s, _EXTEND_MIN_DURATION_S,
            )
            return
        if margin < _EXTEND_MIN_MARGIN_COS:
            logger.debug(
                "[speaker] extend '%s': skip — margin %.3f < %.2f",
                norm, margin, _EXTEND_MIN_MARGIN_COS,
            )
            return

        if existing_rows is not None and len(existing_rows):
            max_sim = float(np.max(existing_rows @ _l2(embedding)))
            if max_sim > _DIVERSITY_COS:
                logger.debug(
                    "[speaker] extend '%s': skip redundant (max_cos=%.3f > %.2f)",
                    norm, max_sim, _DIVERSITY_COS,
                )
                return
        else:
            max_sim = float("nan")

        path = self._write_extended_sample(norm, wav_bytes, embedding)
        if path is None:
            return
        dropped = self._prune_extended(norm)
        self._invalidate_bank()

        if path in dropped:
            logger.debug(
                "[speaker] extend '%s': sample pruned on commit -> %s",
                norm, path.name,
            )
            return
        logger.info(
            "[speaker] extend '%s': ADDED sample (%.1fs, max_cos_to_existing=%s, "
            "margin=%.3f) -> %s",
            norm, duration_s,
            "n/a" if max_sim != max_sim else f"{max_sim:.3f}",
            margin, path.name,
        )

    # embedding-model migration
    #
    # Enrolled embeddings are only comparable to a query embedding from the SAME
    # server model. When the server model changes, stored vectors go stale — but
    # every contributing WAV is retained on disk, so we can re-embed them under
    # the new model WITHOUT asking the user to record again. That turns "the
    # model changed" from "lose every enrollment" into "run a background job".
    # Only users whose WAVs are all gone need a physical re-enroll.

    def _iter_enrolled_users(self) -> list[str]:
        """Normalized names of every user that has a usable voice bank."""
        out: list[str] = []
        if not self._users_dir.is_dir():
            return out
        for entry in sorted(self._users_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if self._has_profile(entry.name):
                out.append(entry.name)
        return out

    def _fetch_server_model_version(self) -> Optional[str]:
        """GET the embedding server's /health → ``audio_embedder_version``.

        Lets the restart reconcile learn the server model WITHOUT sending audio.
        Returns None if the server is unreachable, still booting, or too old to
        report a version — callers then fall back to the lazy recognize-path
        check. /health is plain JSON (not encrypted), behind the same API key.
        """
        if not self._api_url:
            return None
        marker = "/audio-recognizer/embed"
        if self._api_url.endswith(marker):
            health_url = self._api_url[: -len(marker)] + "/health"
        else:
            health_url = self._api_url.rsplit("/", 1)[0] + "/health"
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        try:
            resp = requests.get(health_url, headers=headers, timeout=_API_TIMEOUT_S)
        except requests.RequestException as e:
            logger.info("Reconcile: health unreachable at %s: %s", health_url, e)
            return None
        if resp.status_code != 200:
            logger.info("Reconcile: health returned HTTP %d", resp.status_code)
            return None
        try:
            ver = resp.json().get("audio_embedder_version")
        except ValueError:
            return None
        return str(ver) if ver else None

    def _spawn_startup_reconcile(self) -> None:
        """Start the restart-time model-change check in the background."""
        threading.Thread(
            target=self._startup_reconcile,
            name="speaker-startup-reconcile",
            daemon=True,
        ).start()

    def _startup_reconcile(self) -> None:
        """On restart: if the server model changed while we were down, migrate.

        Best-effort: retries the health probe a few times (the server may still
        be booting), scans metadata cheaply for staleness BEFORE loading the
        heavy preprocessing model, and only then migrates. Any failure is
        swallowed — recognize() is the correctness backstop.
        """
        try:
            ver: Optional[str] = None
            for attempt in range(5):  # 5 tries, ~5s apart, to cover server boot
                ver = self._fetch_server_model_version()
                if ver:
                    break
                if attempt < 4:  # don't sleep after the final attempt
                    time.sleep(5)
            if not ver:
                logger.info(
                    "Startup reconcile: server model version unavailable; "
                    "recognize() will reconcile lazily on the first turn."
                )
                return
            self._server_model_version = ver
            stale = [
                n for n in self._iter_enrolled_users()
                if self._stored_model_version(n) != ver
            ]
            if not stale:
                logger.info(
                    "Startup reconcile: all profiles match server model %s.", ver,
                )
                return
            logger.warning(
                "Startup reconcile: server model %s, %d stale profile(s) %s — "
                "starting background re-embed migration.",
                ver, len(stale), sorted(stale),
            )
            self._start_migration(ver)
        except Exception as e:
            logger.warning("Startup reconcile failed: %s", e)

    def _start_migration(self, server_version: str) -> None:
        """Kick off a background re-embed migration to ``server_version``.

        Single-flight: at most ONE migration runs at a time, regardless of
        version. A request that arrives while one is already running is dropped —
        the next recognize (or the restart reconcile) re-triggers for the
        then-current server version, so a model that changes mid-migration still
        converges without ever running two migrations at once.
        """
        with self._migration_lock:
            if self._migrating_version is not None:
                return
            self._migrating_version = server_version
        try:
            threading.Thread(
                target=self._run_migration,
                args=(server_version,),
                name="speaker-embed-migration",
                daemon=True,
            ).start()
        except Exception:
            # Thread never started — release the flag so a later call can retry
            # (otherwise this version would be blocked from migrating forever).
            with self._migration_lock:
                self._migrating_version = None
            raise

    def _run_migration(self, server_version: str) -> None:
        try:
            self._reconcile_embeddings(server_version)
        except Exception as e:
            logger.warning("Embedding migration to %s aborted: %s", server_version, e)
        finally:
            with self._migration_lock:
                if self._migrating_version == server_version:
                    self._migrating_version = None

    def _reconcile_embeddings(self, server_version: str) -> None:
        """Re-embed every stale profile from its retained WAVs under the new model."""
        migrated = fresh = unmigrated = failed = 0
        for norm in self._iter_enrolled_users():
            # Cheap pre-check to skip already-fresh profiles without taking the
            # per-user lock; _reembed_user re-reads + re-checks under the lock.
            if self._stored_model_version(norm) == server_version:
                fresh += 1
                continue
            try:
                result = self._reembed_user(norm, server_version)
            except EmbeddingAPIUnavailableError as e:
                # Server outage mid-migration: stop and let a later restart /
                # recognize retry. Nothing on disk was corrupted (atomic writes).
                logger.warning(
                    "Migration halted (server unavailable) at %r: %s", norm, e,
                )
                return
            except Exception as e:
                failed += 1
                logger.warning("Migration: %r failed unexpectedly: %s", norm, e)
                continue
            if result is True:
                migrated += 1
            else:  # None → not migrated (removed if all rejected, else left stale)
                unmigrated += 1
        logger.info(
            "Embedding migration to %s done: migrated=%d fresh=%d "
            "unmigrated=%d failed=%d",
            server_version, migrated, fresh, unmigrated, failed,
        )

    def _reembed_user(self, norm: str, server_version: str) -> Optional[bool]:
        """Re-embed one user's retained WAVs. True=migrated; None=not migrated.

        On None the profile is LEFT STALE (excluded from matching) — never
        deleted. This covers both a profile with no source WAVs (legacy
        embedding.npy-only, or WAVs gone) and one whose every retained WAV fails
        the preprocessing gate: a stale profile is harmless (filtered out of
        matching) and its WAVs may be the only copy of the enrollment, so a
        version bump must not destroy it. It re-migrates automatically once a
        sample passes the gate again.

        Rewrites each sample's embedding SIDECAR in place — both the anchor
        (``sample_*.wav``) and extended (``extended_*.wav``) tiers — so the bank
        loader picks up the new-model vectors on its next read. Nothing is
        aggregated: the store is one row per sample, exactly as enroll writes it.

        Takes the per-user lock, which serializes this against another
        ``_reembed_user`` of the same profile — already guaranteed by migration
        single-flight, so it is belt-and-suspenders — AND against a concurrent
        ``enroll()`` of the same user, whose disk-commit holds the same lock, so
        the two never interleave writes to the same profile (sidecars +
        ``metadata.json``). ``metadata.json`` is an atomic write; its sample
        counts/lists are re-derived from disk on read, and both writers stamp the
        same ``embed_model_version``.

        Raises ``EmbeddingAPIUnavailableError`` on outage so the caller can halt
        — the commit runs only after EVERY sample embedded, so a halt leaves the
        profile fully on the old model, never half-migrated.
        """
        with self._user_lock(norm):
            # Re-read fresh: a concurrent enroll (which does not take this lock)
            # or an earlier migration pass may have already stamped this profile
            # to the target version — short-circuit if so.
            meta = self._read_metadata(norm)
            if (meta.get("embed_model_version") or "") == server_version:
                return True

            # Both tiers migrate. A profile with no per-sample WAVs (legacy with
            # only the aggregate embedding.npy, or WAVs deleted) has no audio to
            # re-embed. It is left stale (excluded from matching) until the person
            # re-enrolls — never deleted, since a stale profile is harmless (it is
            # filtered out of matching) and its WAVs may be the only copy of the
            # enrollment. The "all samples fail the gate" case below is handled the
            # same way: left stale, not removed.
            wav_paths = self._anchor_wavs(norm) + self._extended_wavs(norm)
            if not wav_paths:
                logger.warning(
                    "Migration: %r has no retained WAV samples — cannot re-embed; "
                    "left stale (excluded from matching) until re-enrolled.", norm,
                )
                return None

            migrated: list[tuple[Path, np.ndarray]] = []
            rejected: list[Path] = []
            dim = 0
            for wf in wav_paths:
                raw = _read_bytes(str(wf))
                if not raw:
                    continue
                wb = _ensure_wav_16k_mono(raw)
                try:
                    emb = self._call_embedding_api(self._prepare_wav_for_embedding(wb))
                except EmbeddingAPIUnavailableError:
                    raise  # outage → bubble up so migration halts, not a bad sample
                except SpeakerRecognizerError as e:
                    logger.info(
                        "Migration: %r sample %s rejected by gate — %s", norm, wf.name, e,
                    )
                    rejected.append(wf)
                    continue
                migrated.append((wf, emb))
                dim = int(emb.shape[0])

            if not migrated:
                # No retained sample produced an embedding — almost always the
                # gate config tightening (these WAVs passed it at enroll under the
                # old config), a reversible change, or a transient unreadable file.
                # Either way leave the profile STALE rather than deleting its only
                # copy of the audio; it re-migrates automatically once a sample
                # embeds cleanly again.
                logger.warning(
                    "Migration: %r — no retained sample produced an embedding "
                    "(%d gate-rejected of %d); left stale (excluded from "
                    "matching), not deleted.",
                    norm, len(rejected), len(wav_paths),
                )
                return None

            # Commit only now that every sample embedded cleanly. Overwrite each
            # migrated sample's sidecar, and drop the sidecar of any sample the
            # gate rejected so no old-model vector survives — matters for a
            # same-dimension checkpoint swap, where the bank's mixed-dim guard
            # cannot tell an old row from a new one.
            for wf, emb in migrated:
                self._write_sidecar(wf, emb)
            for wf in rejected:
                _sidecar_path(wf).unlink(missing_ok=True)

            meta["embedding_dim"] = dim
            meta["embed_model_version"] = server_version
            meta["updated_at"] = self._now_iso()
            self._write_metadata(norm, meta)
            self._update_registry(norm, meta)
            self._invalidate_bank()  # force recognize to reload the new vectors
            logger.info(
                "Migration: re-embedded %r from %d sample(s) → %s",
                norm, len(migrated), server_version,
            )
            return True

    # --------------------------------------------------------- public: enroll

    def enroll(
        self,
        name: str,
        wav_sources: Iterable[str],
        source_type: str = "base64",
        telegram_username: str = "",
        telegram_id: str = "",
        origin: str = "",
    ) -> dict[str, Any]:
        """Enroll or re-enroll a speaker.

        Each accepted WAV is stored with its own embedding sidecar and becomes
        one row of the user's ANCHOR bank. Nothing is averaged, and no existing
        sample is modified or deleted — a new enrollment can only add rows.

        Unknown-voice clusters belonging to this person are claimed here too,
        but their audio joins the EXTENDED tier (capped, prunable), never the
        anchor tier, so auto-collected audio can never displace the recording
        the user deliberately made.

        Identity (``telegram_username`` / ``telegram_id`` / display name) is
        merged into the SHARED ``/root/local/users/<norm>/metadata.json`` —
        the same file face-enroll writes to — so one person's identity is
        consistent across face, voice, mood, and wellbeing skills.

        Each sample is tagged with its origin (``"mic"`` or ``"telegram"``)
        so a user enrolled only via mic (no Telegram identity yet) can later
        be re-enrolled via Telegram without losing their earlier samples.

        Args:
            name: Display name (normalized to folder-safe lowercase).
            wav_sources: List of base64-encoded WAV data or filepaths.
            source_type: ``"base64"`` or ``"filepath"``.
            telegram_username: Optional Telegram @handle (e.g. ``chloe_92``).
            telegram_id: Optional numeric Telegram user ID.
            origin: ``"mic"`` / ``"telegram"`` / ``"other"``. Auto-derived
                (from presence of telegram_id/username) if empty.

        Returns:
            Metadata dict for the enrolled speaker (voice-specific + merged
            identity fields).
        """
        sources = list(wav_sources or [])
        if not sources:
            raise SpeakerRecognizerError("no audio provided")
        if source_type not in ("base64", "filepath"):
            raise SpeakerRecognizerError(
                f"invalid source_type {source_type!r}"
            )
        if not self.available:
            raise SpeakerRecognizerError(
                "embedding API not configured — set SPEAKER_EMBEDDING_API_URL"
            )

        # Infer origin from whether Telegram identity was supplied.
        if not origin:
            origin = (
                "telegram" if (telegram_username or telegram_id) else "mic"
            )
        # Single tokens only: the tag is embedded as sample_<origin>_<ts>_<uuid>
        # and _sample_origin parses it back with split("_", 2), so an origin
        # containing "_" would read back as its first word and fail the round
        # trip. That is why the web route sends "web" and not "web_device_mic",
        # which used to land here and get silently relabelled "other".
        origin = origin if origin in _SAMPLE_ORIGINS else "other"

        # SPEAKER-DEBUG: per-call latency/memory profile. Enroll runs the
        # preprocessing chain + embedding call once per sample, so the shared
        # stages below aggregate (see `calls` / `ms_max` in the profile).
        self._debug_profile_start("enroll")

        norm = _normalize_label(name)
        logger.info(
            "Enrolling speaker: name=%r (norm=%r) new_samples=%d origin=%s tg_identity=%s",
            name, norm, len(sources), origin,
            bool(telegram_username or telegram_id),
        )
        user_dir = self._users_dir / norm
        user_dir.mkdir(parents=True, exist_ok=True)
        voice_dir = self._voice_dir(norm)
        voice_dir.mkdir(parents=True, exist_ok=True)

        # Persist shared identity early, even if embedding fails later.
        shared_identity = _merge_shared_metadata(
            user_dir,
            display_name=name.strip() or None,
            telegram_username=telegram_username or None,
            telegram_id=telegram_id or None,
        )

        # ------------------------------------------------------------
        # No aggregation: every accepted sample becomes its OWN row in the
        # bank. Work still happens in memory first — validate, embed — so
        # audio that fails the gate never lands on disk. What changed is that
        # committing no longer recomputes a shared vector, which means a new
        # sample can no longer damage an existing one, and nothing already on
        # disk has to be deleted to protect it.
        # ------------------------------------------------------------

        # Step 1 — Decode + normalize incoming audios (in-memory only).
        new_wavs: list[bytes] = []
        with self._debug_stage("decode_input"):  # SPEAKER-DEBUG
            for src in sources:
                if source_type == "filepath":
                    raw = _read_bytes(src)
                else:
                    try:
                        raw = base64.b64decode(src)
                    except Exception as e:
                        raise SpeakerRecognizerError(f"invalid base64: {e}") from e
                if not raw:
                    raise SpeakerRecognizerError("empty audio")
                new_wavs.append(_ensure_wav_16k_mono(raw))

        # Step 2 — Compute embedding per NEW wav BEFORE writing to disk.
        # _prepare_wav_for_embedding raises on too-short/silent audio;
        # _call_embedding_api raises SpeakerRecognizerError for 4xx
        # (audio-level reject — skip this sample) or
        # EmbeddingAPIUnavailableError for network/5xx (bubble up so the
        # whole enroll aborts cleanly and nothing on disk is touched).
        new_embeddings: list[tuple[bytes, np.ndarray]] = []
        per_sample_errors: list[tuple[int, str]] = []
        # SPEAKER-DEBUG: per-rejected-sample gate detail + partial chain output,
        # captured inside the loop because _debug_partial is overwritten by the
        # next sample. Without this the enroll FAIL trace held no audio at all.
        # (index, json detail, the sample's own wav, partial-chain wavs)
        per_sample_debug: list[tuple[int, dict[str, Any], bytes, dict[str, bytes]]] = []
        for idx, wb in enumerate(new_wavs):
            try:
                payload = self._prepare_wav_for_embedding(wb)
                emb = self._call_embedding_api(payload)
                new_embeddings.append((wb, emb))
            except EmbeddingAPIUnavailableError:
                raise
            except SpeakerRecognizerError as e:
                per_sample_errors.append((idx, str(e)))
                if self._debug.enabled:  # SPEAKER-DEBUG
                    detail: dict[str, Any] = {"index": idx, "error": str(e)}
                    gate_detail = getattr(e, "gate_detail", None)
                    if gate_detail is not None:
                        detail["preprocessing_reject"] = gate_detail
                    p_wavs, p_metrics = self._debug_partial_parts()
                    if p_metrics is not None:
                        detail["preprocessing_partial"] = p_metrics
                    per_sample_debug.append((
                        idx, detail, wb,
                        {f"sample_{idx:02d}_{n}": b for n, b in p_wavs.items()},
                    ))
                logger.warning(
                    "Enroll: rejected new sample #%d — %s (not saved to disk)",
                    idx, e,
                )

        if not new_embeddings:
            # Surface the actual reason from perception-service (VAD reject text, etc.)
            # or from local gates (too short / silent) — no hardcoded summary.
            if self._debug.enabled:  # SPEAKER-DEBUG
                fail_wavs: dict[str, bytes] = {}
                for idx, _detail, wb, partial_wavs in per_sample_debug:
                    fail_wavs[f"sample_{idx:02d}_input.wav"] = wb
                    fail_wavs.update(partial_wavs)
                self._debug.record(
                    "enroll", reason="no-valid-samples",
                    wavs=fail_wavs,
                    result={
                        "name": norm, "origin": origin, "num_new": len(new_wavs),
                        # Now carries the structured gate reason + the partial
                        # chain output per sample, not just the message string.
                        "per_sample_errors": [d for _i, d, _w, _p in per_sample_debug],
                    },
                    profile=self._debug_profile_dict(),
                )
            if len(per_sample_errors) == 1:
                raise SpeakerRecognizerError(per_sample_errors[0][1])
            details = "; ".join(
                f"sample #{i}: {msg}" for i, msg in per_sample_errors
            )
            raise SpeakerRecognizerError(f"no valid new samples — {details}")

        # Step 3 — Mutual-match gate WITHIN the incoming batch.
        #
        # Every clip must clear _MATCH_COS against at least ONE OTHER clip in
        # the batch. No clip is privileged: a batch assembled from a voice_<N>
        # cluster has no "correct" sample to anchor on, so electing one and
        # judging the rest against it just moves the guesswork rather than
        # removing it — if the elected clip is the wrong person, the right ones
        # get dropped as incoherent and the wrong one is enrolled permanently.
        #
        # Requiring a partner instead lets natural variation through (a clip
        # only has to agree with SOMETHING, not with a single chosen yardstick)
        # while still ejecting true outliers, which by definition agree with
        # nothing. Note this is degree >= 1, not one connected component: two
        # disjoint pairs both survive.
        #
        # Single-sample enrolls skip the gate entirely — there is no pair to
        # form, and the caller deliberately offered that one recording.
        durations = [_wav_duration_s(wb) for wb, _e in new_embeddings]
        anchors: list[tuple[bytes, np.ndarray]] = []
        dropped_new = 0

        if len(new_embeddings) == 1:
            anchors = list(new_embeddings)
        else:
            rows = np.stack([_l2(e) for _w, e in new_embeddings], axis=0)
            sims = rows @ rows.T                     # [N, N] raw cosine
            np.fill_diagonal(sims, -np.inf)          # a clip cannot partner itself
            best_other = sims.max(axis=1)            # closest OTHER clip per row
            for i, (wb, emb) in enumerate(new_embeddings):
                if best_other[i] >= _MATCH_COS:
                    anchors.append((wb, emb))
                else:
                    dropped_new += 1
                    logger.info(
                        "Enroll: dropped new sample #%d (%.1fs) — best match to any "
                        "other sample in the batch is cos=%.3f < %.2f (no partner)",
                        i, durations[i], float(best_other[i]), _MATCH_COS,
                    )

        if not anchors:
            # Nothing agreed with anything: the batch is not one speaker (or is
            # all noise). Fail loudly rather than committing an arbitrary clip
            # as a permanent anchor — the caller can ask for a cleaner sample.
            raise SpeakerRecognizerError(
                f"no coherent samples — none of the {len(new_embeddings)} clips "
                f"matched another at cos >= {_MATCH_COS}; audio may contain more "
                f"than one speaker"
            )

        # Step 4 — Claim unknown-voice clusters that belong to this person.
        #
        # Two ways in, unioned:
        #   (a) Explicit — a source path lives inside a ``voice_<N>`` dir.
        #       Passing ANY path from a cluster claims the whole cluster, so an
        #       agent that surfaces one sample per turn strands nothing.
        #   (b) Match — the cluster scores >= _MATCH_COS against the anchors we
        #       just accepted. This previously ran at a deliberately looser
        #       0.625 scaled bar; it now clears exactly the bar that
        #       recognizing this person clears, so nothing enters an enrollment
        #       that would not have been called this person at recognize time.
        #
        # Filepath sources only — base64 carries no path to resolve.
        claimed: list[tuple[bytes, np.ndarray]] = []
        consume_hashes: list[str] = []
        if source_type == "filepath":
            try:
                unknown_root = _UNKNOWN_AUDIO_DIR.resolve()
            except OSError:
                unknown_root = None
            claimed_hashes: set[str] = set()
            if unknown_root is not None:
                for src in sources:
                    try:
                        resolved = Path(src).resolve()
                        resolved.relative_to(unknown_root)
                    except (OSError, ValueError):
                        continue
                    if _VOICE_STRANGER_DIR_RE.match(resolved.parent.name):
                        claimed_hashes.add(resolved.parent.name)

            # Paths the caller handed us are ALREADY enrolled as anchors above.
            # Claiming a cluster pulls its whole directory, which re-globs those
            # same files — without this set they would be embedded a second time
            # and stored again in the extended tier, so one clip would occupy two
            # bank rows carrying identical information. Resolved rather than
            # string-compared so a caller path and a glob result that differ only
            # in form still match.
            caller_resolved: set[str] = set()
            for src in sources:
                try:
                    caller_resolved.add(str(Path(src).resolve()))
                except OSError:
                    continue

            anchor_rows = np.stack([_l2(e) for _w, e in anchors], axis=0)
            matched_hashes = set(
                self._match_stranger_clusters(anchor_rows, _MATCH_COS)
            )
            consume_hashes = sorted(claimed_hashes | matched_hashes)
            if claimed_hashes:
                logger.info(
                    "Cluster claim: source paths claimed %d cluster(s) %s",
                    len(claimed_hashes), sorted(claimed_hashes),
                )

            for h in consume_hashes:
                cluster_dir_path = _UNKNOWN_AUDIO_DIR / h
                if not cluster_dir_path.is_dir():
                    continue
                for wav in sorted(cluster_dir_path.glob("*.wav")):
                    wav_str = str(wav)
                    try:
                        if str(wav.resolve()) in caller_resolved:
                            continue  # already handled as an anchor above
                    except OSError:
                        pass
                    try:
                        raw = _read_bytes(wav_str)
                    except OSError as e:
                        logger.warning("Cluster claim: cannot read %s — %s", wav_str, e)
                        continue
                    try:
                        wb = _ensure_wav_16k_mono(raw)
                        payload = self._prepare_wav_for_embedding(wb)
                        emb = self._call_embedding_api(payload)
                    except EmbeddingAPIUnavailableError:
                        raise
                    except SpeakerRecognizerError as e:
                        logger.info("Cluster claim: skip %s — %s", wav.name, e)
                        continue
                    claimed.append((wb, emb))
                    if wav_str not in sources:
                        sources.append(wav_str)
            if consume_hashes:
                logger.info(
                    "Cluster claim: %d WAV(s) from %d cluster(s) %s "
                    "(claimed=%d, matched=%d, threshold=%.2f) -> extended tier",
                    len(claimed), len(consume_hashes), consume_hashes,
                    len(claimed_hashes), len(matched_hashes - claimed_hashes),
                    _MATCH_COS,
                )

        # Step 5 — Backfill sidecars for any pre-existing sample that lacks one.
        #
        # Covers two cases without a migration script: a profile enrolled
        # before the bank rewrite (aggregated embedding.npy, no sidecars), and
        # a sample whose WAV was written but whose sidecar was not. Strictly
        # best-effort — a failure just leaves the sample unbacked, and NOTHING
        # is deleted. Once backfilled, the legacy embedding.npy stops being
        # consulted (see _load_user_bank).
        backfilled = 0
        for p in self._anchor_wavs(norm):
            if _sidecar_path(p).is_file():
                continue
            try:
                emb = self._call_embedding_api(
                    self._prepare_wav_for_embedding(p.read_bytes())
                )
                np.save(_sidecar_path(p), _l2(emb))
                backfilled += 1
            except EmbeddingAPIUnavailableError:
                raise
            except (SpeakerRecognizerError, OSError) as e:
                logger.info(
                    "Enroll: no sidecar for existing sample %s — %s (kept)",
                    p.name, e,
                )
        if backfilled:
            logger.info("Enroll: backfilled %d sidecar(s) for %s", backfilled, norm)
        # Opposite direction: drop sidecars whose WAV was deleted outside HAL.
        self._reconcile_sidecars(norm)

        # Step 6 — Commit anchors. Each WAV gets its embedding sidecar written
        # first-class alongside it. The millisecond stamp is offset by index so
        # two samples in the same enroll can never collide, and lexical order
        # stays chronological (the old code recomputed time.time() per file
        # with a comment claiming a sleep kept them unique — there was no
        # sleep, and same-batch samples routinely shared a timestamp).
        # Steps 6–8 mutate this user's on-disk voice profile. Hold the per-user
        # lock across the WHOLE commit so it cannot interleave with a background
        # migration of the SAME user: _reembed_user may rmtree this voice dir
        # (its "all samples rejected by the gate" removal path) or rewrite its
        # sidecars, and a half-applied enroll racing that delete would silently
        # lose the freshly recorded samples. Only the disk commit is guarded —
        # the embedding network calls above (Steps 2/4/5) ran lock-free, so a
        # normal enroll blocks on this lock only when this exact user is being
        # re-embedded right now, and only for that one profile's re-embed, never
        # the whole migration batch.
        with self._user_lock(norm):
            written_new_paths: list[Path] = []
            stamp = int(time.time() * 1000)
            for i, (wb, emb) in enumerate(anchors):
                fname = f"sample_{origin}_{stamp + i}_{uuid.uuid4().hex[:8]}.wav"
                fpath = voice_dir / fname
                try:
                    fpath.write_bytes(wb)
                    np.save(_sidecar_path(fpath), _l2(emb))
                except OSError as e:
                    logger.warning("Enroll: failed to write %s: %s", fpath, e)
                    continue
                written_new_paths.append(fpath)

            if not written_new_paths and not self._has_profile(norm):
                raise SpeakerRecognizerError("failed to write any enrollment sample")

            # Step 7 — Commit claimed cluster audio to the EXTENDED tier, then
            # prune that tier back to its cap by diversity. Written first and
            # pruned after (rather than admission-tested up front like the face
            # pipeline does) because enroll is not a hot path and the churn is
            # bounded by one cluster's worth of files.
            for wb, emb in claimed:
                self._write_extended_sample(norm, wb, emb)
            if claimed:
                self._prune_extended(norm)

            self._invalidate_bank()

            # Re-read from disk so metadata reflects exactly what is stored.
            anchor_paths = self._anchor_wavs(norm)
            extended_paths = self._extended_wavs(norm)
            anchor_embs, _ = self._read_tier(anchor_paths)
            extended_embs, _ = self._read_tier(extended_paths)
            dim = int(anchor_embs[0].shape[0]) if anchor_embs else 0

            logger.info(
                "Enroll committed: anchors_written=%d anchors_rejected=%d "
                "batch_dropped=%d claimed_to_extended=%d "
                "total_anchors=%d total_extended=%d dim=%d",
                len(written_new_paths), len(per_sample_errors), dropped_new,
                len(claimed), len(anchor_paths), len(extended_paths), dim,
            )

            # Update voice metadata + registry.
            existing = self._read_metadata(norm)
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            enrollment_sources = sorted(
                {_sample_origin(p.name) for p in anchor_paths} | {origin}
            )
            meta: dict[str, Any] = {
                "name": norm,
                "display_name": shared_identity.get("display_name")
                    or existing.get("display_name")
                    or name.strip()
                    or norm,
                "telegram_username": shared_identity.get("telegram_username", ""),
                "telegram_id": shared_identity.get("telegram_id", ""),
                "has_telegram_identity": bool(
                    shared_identity.get("telegram_id")
                    or shared_identity.get("telegram_username")
                ),
                "enrollment_sources": enrollment_sources,
                "last_enrollment_source": origin,
                "enrolled_at": existing.get("enrolled_at", now_iso),
                "updated_at": now_iso,
                # num_samples keeps its meaning: samples the user enrolled.
                # Auto-collected audio is counted separately so the two can never
                # be confused in the UI or by a skill.
                "num_samples": len(anchor_paths),
                "sample_files": [p.name for p in anchor_paths],
                "sample_origins": {p.name: _sample_origin(p.name) for p in anchor_paths},
                "num_extended": len(extended_paths),
                "extended_files": [p.name for p in extended_paths],
                "embedding_dim": dim,
                "embed_model_version": self._server_model_version or "",
            }
            self._write_metadata(norm, meta)
            self._update_registry(norm, meta)

            # Drop any stranger clusters whose WAVs were just claimed. Keeping
            # them would leave stale rows that re-label the now-known speaker as
            # voice_<N> on any recognition below the match threshold.
            if source_type == "filepath":
                self._drop_consumed_clusters(sources)

        logger.info(
            "Enrolled speaker '%s' — %d anchor + %d extended sample(s), dim=%d",
            norm, meta["num_samples"], meta["num_extended"], dim,
        )
        if self._debug.enabled:  # SPEAKER-DEBUG
            # cohesion = mean cosine of every stored anchor to the reference
            # sample; a single "how tight is this enrollment" number. There is
            # no aggregated vector to measure against any more.
            try:
                if len(anchor_embs) > 1:
                    _st = np.stack([_l2(e) for e in anchor_embs], axis=0)
                    _sm = _st @ _st.T
                    _n = len(anchor_embs)
                    # Mean of the off-diagonal: how tightly the stored anchors
                    # agree with each other. No reference sample exists now, so
                    # there is nothing else meaningful to measure against.
                    cohesion = round(
                        float((_sm.sum() - np.trace(_sm)) / (_n * (_n - 1))), 4
                    )
                else:
                    cohesion = None
            except Exception:
                cohesion = None
            self._debug.record(
                "enroll", cls=norm, confidence=cohesion,
                wavs={
                    f"anchor_new_{i:02d}.wav": wb
                    for i, (wb, _e) in enumerate(anchors)
                },
                arrays={
                    "anchor_bank.npy": (
                        np.stack(anchor_embs, axis=0) if anchor_embs
                        else np.zeros((0, 0), dtype=np.float32)
                    ),
                    "extended_bank.npy": (
                        np.stack(extended_embs, axis=0) if extended_embs
                        else np.zeros((0, 0), dtype=np.float32)
                    ),
                },
                result={
                    "name": norm, "cohesion": cohesion, "origin": origin,
                    "match_threshold": _MATCH_COS,
                    "num_new": len(new_wavs),
                    "num_anchors_written": len(written_new_paths),
                    "num_new_rejected_by_server": len(per_sample_errors),
                    "num_new_dropped_no_partner": dropped_new,
                    "batch_durations_s": [round(d, 3) for d in durations],
                    "num_claimed_to_extended": len(claimed),
                    "claimed_clusters": consume_hashes,
                    "sidecars_backfilled": backfilled,
                    "num_anchors_total": len(anchor_paths),
                    "num_extended_total": len(extended_paths),
                    "embedding_dim": dim,
                    "per_sample_errors": [
                        {"index": i, "error": m} for i, m in per_sample_errors
                    ],
                },
                # Stages aggregate across every sample this enroll embedded.
                profile=self._debug_profile_dict(),
            )
        return meta

    def drop_stranger_cluster(self, label: str) -> bool:
        """Drop a single stranger cluster by label.

        Removes the centroid row from the in-memory tables (persisting the
        reduced tables), then ``rmtree`` the on-disk cluster sub-dir.
        Returns ``True`` if anything was removed, ``False`` when the label
        wasn't known and no dir existed (route uses that for 404).
        """
        if not label or not _VOICE_STRANGER_DIR_RE.match(label):
            return False
        removed_centroid = False
        with self._stranger_lock:
            if (
                self._stranger_labels is not None
                and self._stranger_embeds is not None
                and len(self._stranger_labels) > 0
            ):
                mask = np.array(
                    [lbl != label for lbl in self._stranger_labels],
                    dtype=bool,
                )
                if not mask.all():
                    self._stranger_embeds = self._stranger_embeds[mask]
                    self._stranger_labels = self._stranger_labels[mask]
                    self._save_strangers()
                    removed_centroid = True
        removed_dir = False
        cluster_dir = _UNKNOWN_AUDIO_DIR / label
        if cluster_dir.is_dir():
            try:
                shutil.rmtree(cluster_dir)
                removed_dir = True
            except OSError as e:
                logger.warning(
                    "drop_stranger_cluster %s: rmtree failed: %s", label, e,
                )
        if removed_centroid or removed_dir:
            logger.info(
                "drop_stranger_cluster %s (centroid=%s, dir=%s)",
                label, removed_centroid, removed_dir,
            )
        return removed_centroid or removed_dir

    def _drop_consumed_clusters(self, wav_paths: list[str]) -> None:
        """Remove stranger clusters whose WAVs were consumed by enroll().

        Looks at each ``wav_path``'s parent dir — if it's a ``voice_<N>``
        sub-dir inside ``SPEAKER_UNKNOWN_AUDIO_DIR``, that cluster is now
        redundant (the speaker is known) and we drop both:
          - centroid row from ``_stranger_embeds`` / ``_stranger_labels``,
          - the cluster sub-dir on disk.

        Safe no-op if the caller passed paths from outside the cluster tree
        (e.g. Telegram enroll writes into a session temp dir).
        """
        try:
            unknown_root = _UNKNOWN_AUDIO_DIR.resolve()
        except OSError:
            return
        consumed: set[str] = set()
        for p in wav_paths:
            try:
                resolved = Path(p).resolve()
                resolved.relative_to(unknown_root)
            except (OSError, ValueError):
                continue
            parent_name = resolved.parent.name
            if _VOICE_STRANGER_DIR_RE.match(parent_name):
                consumed.add(parent_name)
        if not consumed:
            return

        with self._stranger_lock:
            if (
                self._stranger_labels is not None
                and self._stranger_embeds is not None
                and len(self._stranger_labels) > 0
            ):
                mask = np.array(
                    [lbl not in consumed for lbl in self._stranger_labels],
                    dtype=bool,
                )
                if not mask.all():
                    self._stranger_embeds = self._stranger_embeds[mask]
                    self._stranger_labels = self._stranger_labels[mask]
                    self._save_strangers()
                    logger.info(
                        "Enroll: dropped %d stranger centroid(s) %s after enrollment",
                        int((~mask).sum()), sorted(consumed),
                    )

        for label in consumed:
            cluster_dir = _UNKNOWN_AUDIO_DIR / label
            if not cluster_dir.is_dir():
                continue
            try:
                shutil.rmtree(cluster_dir)
                logger.info("Enroll: removed cluster dir %s", cluster_dir)
            except OSError as e:
                logger.warning(
                    "Enroll: failed to remove cluster dir %s: %s", cluster_dir, e,
                )

    # --------------------------------------------------------- public: remove

    def remove(self, name: str) -> bool:
        """Delete the user's voice folder (embedding + samples + voice metadata).

        Other per-user data (face photos, mood, wellbeing, ...) is preserved —
        we only touch the ``voice/`` subdir. The SHARED identity file
        ``/root/local/users/<norm>/metadata.json`` (telegram_username,
        telegram_id) is left untouched because face-enroll and other skills
        may still depend on it.
        """
        norm = _normalize_label(name)
        voice_dir = self._voice_dir(norm)
        if not voice_dir.is_dir():
            self._remove_from_registry(norm)
            return False
        try:
            shutil.rmtree(voice_dir)
        except OSError as e:
            logger.warning("failed to remove voice dir for %s: %s", norm, e)
            return False
        self._remove_from_registry(norm)
        logger.info("Removed speaker '%s'", norm)
        return True

    # ------------------------------------------------------ public: recognize

    def _debug_safe_assign_hash(  # SPEAKER-DEBUG (remove before deploy)
        self,
        query_chunks: np.ndarray,
        wav_bytes: bytes,
        *,
        resolved_name: str,
        is_match: bool,
        num_enrolled: int,
        source_type: str,
        saved_path: str,
    ) -> Optional[str]:
        """SPEAKER-DEBUG wrapper around _assign_voiceprint_hash.

        Stranger clustering can raise on an embedding-dimension mismatch between
        the stored voice_strangers store and the current backend (e.g. the
        192-vs-256 concatenate error when the embedding model changed). That
        raise happens BEFORE recognize()'s trace hook, so the failure otherwise
        never lands in the logs. This wrapper captures it as a FAIL trace with
        the input audio + dims + error, and degrades to no-cluster so recognize
        returns 'unknown' instead of crashing. Original call was just
        `self._assign_voiceprint_hash(query_chunks)`.
        """
        try:
            return self._assign_voiceprint_hash(query_chunks) or None
        except Exception as e:
            logger.warning("Recognize: voiceprint hash assignment failed — %s", e)
            if self._debug.enabled:
                dur, rms = _debug_audio_stats(wav_bytes)
                self._debug.record(
                    "recognize", reason="stranger-assign-error",
                    wavs={"input.wav": wav_bytes},
                    arrays={"input_chunks.npy": query_chunks},
                    result={
                        "error": repr(e), "name": resolved_name, "match": is_match,
                        "num_enrolled": num_enrolled,
                        "num_query_chunks": int(query_chunks.shape[0]),
                        "embedding_dim": int(query_chunks.shape[1]),
                        "duration_s": dur, "rms": rms, "source_type": source_type,
                        "unknown_audio_path": saved_path,
                    },
                )
            return None

    def recognize(
        self,
        wav_source: str,
        source_type: str = "base64",
    ) -> dict[str, Any]:
        """Recognize a speaker from a single WAV audio.

        Returns a dict with:

        ``name``
            matched user's normalized label, or ``"unknown"``
        ``confidence``
            best match confidence in ``[0, 1]``
        ``match``
            whether the best confidence exceeds ``match_threshold``
        ``unknown_audio_path``
            path to the audio saved under the unknown-audio dir (always set —
            so the skill can reuse the path for later enrollment)
        ``candidates``
            top-3 ``(name, confidence)`` pairs for debugging
        """
        if source_type not in ("base64", "filepath"):
            raise SpeakerRecognizerError(
                f"invalid source_type {source_type!r}"
            )

        logger.info(
            "Recognize start: source_type=%s source=%s",
            source_type,
            wav_source if source_type == "filepath" else f"<base64 {len(wav_source)}B>",
        )

        # SPEAKER-DEBUG: start the per-call latency/memory profile FIRST, so the
        # decode below is inside it. Reset per call, like the snapshots after it.
        self._debug_profile_start("recognize")

        with self._debug_stage("decode_input"):
            if source_type == "filepath":
                raw = _read_bytes(wav_source)
            else:
                try:
                    raw = base64.b64decode(wav_source)
                except Exception as e:
                    raise SpeakerRecognizerError(f"invalid base64: {e}") from e
            if not raw:
                raise SpeakerRecognizerError("empty audio")

            wav_bytes = _ensure_wav_16k_mono(raw)
        # SPEAKER-DEBUG: clear the per-call stranger snapshot so a trace can
        # never report the PREVIOUS call's cluster score when this call's
        # clustering is skipped (empty/zero-norm chunks) or fails.
        self._debug_stranger = None
        self._debug_preproc = None  # SPEAKER-DEBUG: reset per-call preprocessing snapshot

        with self._debug_stage("save_input_wav"):
            saved_path = self._save_incoming_audio(wav_bytes)

        if not self.available:
            if not self._logged_unavailable:
                logger.warning("Embedding server not configured — set SPEAKER_EMBEDDING_API_URL or dl_base_url; speaker-ID returning 'unknown' until configured")
                self._logged_unavailable = True
            if self._debug.enabled:  # SPEAKER-DEBUG
                self._debug.record(
                    "recognize", reason="api-not-configured",
                    wavs={"input.wav": wav_bytes},
                    result={
                        "source_type": source_type,
                        "error": "embedding API not configured",
                    },
                    profile=self._debug_profile_dict(),
                )
            return {
                "name": "unknown",
                "confidence": 0.0,
                "match": False,
                "unknown_audio_path": saved_path,
                "voiceprint_hash": None,
                "candidates": [],
                "error": "embedding API not configured",
            }

        try:
            payload = self._prepare_wav_for_embedding(wav_bytes)
            # Per-chunk query embeddings — same per-chunk granularity that
            # perception-service's /recognize uses internally, so per-chunk voting
            # below produces apples-to-apples confidence.
            query_chunks = self._call_embedding_api(
                payload, use_sliding_window=True
            )  # [M, D]
        except SpeakerRecognizerError as e:
            logger.warning(
                "Recognize: embedding failed for %s — %s", saved_path, e,
            )
            if self._debug.enabled:  # SPEAKER-DEBUG
                dur, rms = _debug_audio_stats(wav_bytes)
                fail_result = {
                    "source_type": source_type, "error": str(e),
                    "duration_s": dur, "rms": rms,
                    "vad_min_duration_s": config.SPEAKER_PROC_VAD_MIN_DURATION_SEC,
                    "unknown_audio_path": saved_path,
                }
                # On-device preprocessing-gate rejection (VAD / STOI / quality):
                # attach the structured reason + metrics (incl. stoi_score).
                gate_detail = getattr(e, "gate_detail", None)
                if gate_detail is not None:
                    fail_result["preprocessing_reject"] = gate_detail
                # Plus the audio the chain had produced when the gate said no —
                # for a STOI reject that is the TEN-VAD output, i.e. the very
                # clip the rejection is about. Without it the trace held only
                # the raw input and there was nothing to listen to.
                partial_wavs, partial_metrics = self._debug_partial_parts()
                if partial_metrics is not None:
                    fail_result["preprocessing_partial"] = partial_metrics
                self._debug.record(
                    "recognize", reason=self._debug.classify_reason(e),
                    wavs={"input.wav": wav_bytes, **partial_wavs},
                    result=fail_result,
                    # Latency/memory up to the failure — a gate reject still paid
                    # for TEN-VAD (and STOI, if it got that far).
                    profile=self._debug_profile_dict(),
                )
            return {
                "name": "unknown",
                "confidence": 0.0,
                "match": False,
                "unknown_audio_path": saved_path,
                "voiceprint_hash": None,
                "candidates": [],
                "error": str(e),
            }

        logger.info(
            "Recognize: query embedding chunks=%d dim=%d saved=%s",
            int(query_chunks.shape[0]), int(query_chunks.shape[1]), saved_path,
        )

        with self._debug_stage("load_enrolled"):
            bank_rows, bank_labels, bank_tiers = self._load_bank()
            known = sorted(set(bank_labels))

        # Per-turn model-identity gate. A stored row is only comparable to this
        # query when it came from the SAME server model — the /embed above just
        # refreshed _server_model_version. Any profile whose stamped version
        # differs is EXCLUDED from this turn's match (so a model swap can't
        # wrong-match, or crash the matmul on a dim change; it reads as unknown
        # until re-embedded), and a one-shot background re-embed is kicked to
        # bring stale profiles current. That migration is single-flight and runs
        # in a daemon thread — the main flow pays only this cheap in-memory
        # filter, never the re-embed itself.
        if bank_rows is not None and known:
            server_ver = self._server_model_version
            if server_ver:
                stale = {
                    n for n in known if self._stored_model_version(n) != server_ver
                }
                if stale:
                    logger.warning(
                        "Recognize: server model %s but %d stale profile(s) %s — "
                        "excluded this turn; starting background re-embed migration.",
                        server_ver, len(stale), sorted(stale),
                    )
                    self._start_migration(server_ver)
                    keep = [i for i, lb in enumerate(bank_labels) if lb not in stale]
                    bank_rows = bank_rows[keep] if keep else None
                    bank_labels = [bank_labels[i] for i in keep]
                    bank_tiers = [bank_tiers[i] for i in keep]
                    known = sorted(set(bank_labels))
            # Defensive dim guard for when the server reports no version (gate
            # above skipped): _load_bank makes every row one uniform width, so a
            # bank whose width != the query's is wholly incomparable and would
            # crash the matmul below — drop it and fall through to unknown.
            if bank_rows is not None and int(bank_rows.shape[1]) != int(
                query_chunks.shape[1]
            ):
                logger.warning(
                    "Recognize: bank dim %d != query dim %d — excluded (model likely changed).",
                    int(bank_rows.shape[1]), int(query_chunks.shape[1]),
                )
                bank_rows, bank_labels, bank_tiers, known = None, [], [], []

        if bank_rows is None or not known:
            # No enrolled users — every voice is unknown. Still assign a
            # stable cluster hash so repeat speakers can be tracked before
            # anyone is enrolled.
            logger.info("Recognize: no enrolled users — unknown + cluster-only path")
            with self._debug_stage("stranger_cluster"):
                vp_hash = self._debug_safe_assign_hash(  # SPEAKER-DEBUG (was: _assign_voiceprint_hash)
                    query_chunks, wav_bytes, resolved_name="unknown", is_match=False,
                    num_enrolled=0, source_type=source_type, saved_path=saved_path,
                )
            saved_path = self._move_to_cluster(saved_path, vp_hash)
            logger.info(
                "Recognize result: name=unknown confidence=0.00 cluster=%s path=%s",
                vp_hash or "(none)", saved_path,
            )
            if self._debug.enabled:  # SPEAKER-DEBUG
                dur, rms = _debug_audio_stats(wav_bytes)
                st = self._debug_stranger or {}
                st_score = st.get("score")
                pp_wavs, pp_metrics = self._debug_preproc_parts()
                self._debug.record(
                    # No enrolled users → dir named by the stranger cluster and
                    # its match score (how sure this is the same returning voice).
                    "recognize",
                    cls=_debug_stranger_label(vp_hash),
                    confidence=(st_score if st_score is not None else 0.0),
                    wavs={"input.wav": wav_bytes, **pp_wavs},
                    arrays={
                        "input_chunks.npy": query_chunks,
                        "input_embedding.npy": _l2(query_chunks.mean(axis=0)),
                    },
                    result={
                        "name": "unknown", "match": False,
                        "threshold": self._match_threshold,
                        "voiceprint_hash": vp_hash or None,
                        "stranger_match_score": (round(st_score, 4) if st_score is not None else None),
                        "stranger_reappeared": st.get("reappeared"),
                        "closest_cluster": st.get("closest_label"),
                        "num_stranger_clusters": st.get("num_clusters"),
                        "num_enrolled": 0,
                        "num_query_chunks": int(query_chunks.shape[0]),
                        "embedding_dim": int(query_chunks.shape[1]),
                        "candidates": [], "source_type": source_type,
                        "duration_s": dur, "rms": rms,
                        "preprocessing": pp_metrics,
                        "unknown_audio_path": saved_path,
                    },
                    profile=self._debug_profile_dict(),
                )
            return {
                "name": "unknown",
                "confidence": 0.0,
                "match": False,
                "unknown_audio_path": saved_path,
                "voiceprint_hash": vp_hash or None,
                "candidates": [],
            }

        # Per-chunk voting (mirrors perception-service.recognize line 614-645):
        # for each query chunk, pick the highest-confidence speaker, record
        # one vote and one confidence sample. Winner = most votes, tiebreak
        # by avg confidence. Returned confidence = avg of winner's votes.
        with self._debug_stage("match_vote"):
            names = list(known)
            # Score every chunk against every ROW, then collapse each speaker's
            # rows to their best. A speaker holds several independent samples
            # now, so "how well does this chunk match Leo" is "how well does it
            # match Leo's closest sample" — the same max-over-bank reduction
            # faceid/recognizer.py:787 does across its upload and extended
            # banks. Values are raw cosine throughout; nothing is rescaled.
            #
            # Note this makes the score monotonically non-decreasing in bank
            # size: more rows means more chances at a high draw, for impostors
            # too. _MAX_EXTENDED_SAMPLES is what bounds that drift.
            row_sims = query_chunks @ bank_rows.T          # [M, N rows] raw cos
            label_arr = np.asarray(bank_labels)
            confs = np.stack(
                [row_sims[:, label_arr == n].max(axis=1) for n in names],
                axis=1,
            )                                              # [M, K] raw cos
            best_idx = confs.argmax(axis=1)                             # [M]
            best_conf_per_chunk = confs[np.arange(confs.shape[0]), best_idx]

            vote_count: dict[str, int] = {}
            conf_sum: dict[str, float] = {}
            for k_idx, c in zip(best_idx.tolist(), best_conf_per_chunk.tolist()):
                n = names[k_idx]
                vote_count[n] = vote_count.get(n, 0) + 1
                conf_sum[n] = conf_sum.get(n, 0.0) + float(c)

            # Tiebreak by avg confidence so a 1-vote winner with high conf
            # never beats a 5-vote one — votes dominate.
            ranked = sorted(
                vote_count.keys(),
                key=lambda n: (vote_count[n], conf_sum[n] / vote_count[n]),
                reverse=True,
            )
            best_name = ranked[0]
            best_conf = conf_sum[best_name] / vote_count[best_name]
            scores = [
                (n, conf_sum[n] / vote_count[n], vote_count[n]) for n in ranked
            ]

        is_match = best_conf >= self._match_threshold
        resolved_name = best_name if is_match else "unknown"

        # Full per-speaker breakdown — lets operator see why a near-miss
        # happened (e.g. speaker_a scored 0.68 with 5 votes vs speaker_b 0.64
        # with 4 votes against threshold 0.70 → both lose, tag as unknown).
        scores_str = ", ".join(
            f"{n}={c:.3f}(v={v})" for n, c, v in scores[:5]
        )
        logger.info(
            "Recognize scores: threshold=%.2f match=%s -> name=%s | %s",
            self._match_threshold, is_match, resolved_name, scores_str,
        )

        # Only assign a stranger cluster hash for unknowns — known speakers
        # already have a stable identity (their name).
        with self._debug_stage("stranger_cluster"):
            vp_hash = None if is_match else self._debug_safe_assign_hash(  # SPEAKER-DEBUG (was: _assign_voiceprint_hash)
                query_chunks, wav_bytes, resolved_name=resolved_name, is_match=is_match,
                num_enrolled=len(known), source_type=source_type, saved_path=saved_path,
            )
        # Move WAV into per-cluster sub-dir so later inspection can group
        # samples by cluster. Known-speaker WAVs stay in the flat dir.
        if vp_hash:
            saved_path = self._move_to_cluster(saved_path, vp_hash)

        # Auto-extend: a confidently-recognized turn may earn a slot in the
        # speaker's extended tier, so the bank picks up the acoustics this
        # room actually produces (distance, loudness) instead of only the one
        # enrollment recording. Gated hard inside _maybe_extend_user — most
        # turns are rejected as too short, too close a tie, or redundant.
        if is_match:
            with self._debug_stage("auto_extend"):
                margin = (
                    best_conf - scores[1][1] if len(scores) > 1 else float("inf")
                )
                try:
                    # This speaker's slice of the bank we already matched
                    # against — no reason to read their sidecars back off disk.
                    own_rows = bank_rows[label_arr == best_name]
                    self._maybe_extend_user(
                        best_name,
                        _l2(query_chunks.mean(axis=0)),
                        wav_bytes,
                        existing_rows=own_rows,
                        duration_s=_wav_duration_s(wav_bytes),
                        margin=float(margin),
                    )
                except Exception as e:
                    # Never let bank maintenance break a turn — the identity
                    # decision is already made and the reply depends on it.
                    logger.warning("auto-extend failed for %s: %s", best_name, e)

        logger.info(
            "Recognize result: name=%s confidence=%.3f match=%s cluster=%s path=%s",
            resolved_name, best_conf, is_match, vp_hash or "(none)", saved_path,
        )
        result: dict[str, Any] = {
            "name": resolved_name,
            "confidence": round(best_conf, 4),
            "match": is_match,
            "unknown_audio_path": saved_path,
            "voiceprint_hash": vp_hash,
            "candidates": [
                {"name": n, "confidence": round(c, 4), "votes": v}
                for n, c, v in scores[:3]
            ],
        }
        # Surface identity fields on match.
        if is_match:
            shared = self._read_shared_metadata(best_name)
            result["display_name"] = shared.get("display_name", best_name)
            result["telegram_username"] = shared.get("telegram_username", "")
            result["telegram_id"] = shared.get("telegram_id", "")
            result["has_telegram_identity"] = bool(
                shared.get("telegram_id") or shared.get("telegram_username")
            )
        if self._debug.enabled:  # SPEAKER-DEBUG
            dur, rms = _debug_audio_stats(wav_bytes)
            # Full per-chunk comparison across EVERY enrolled speaker. Voting keeps
            # only each chunk's argmax winner, so a speaker that never wins a chunk
            # gets 0 votes and vanishes from `candidates` — even though it WAS
            # compared on every chunk. Capture the whole [chunk x speaker] matrix
            # so you can see the losers and why each chunk voted the way it did.
            _confs = confs.tolist()  # [M chunks][K speakers], RAW cosine [-1, 1]
            per_chunk_scores = [
                {
                    "chunk": ci,
                    "winner": names[int(best_idx[ci])],
                    "scores": {names[k]: round(_confs[ci][k], 4) for k in range(len(names))},
                }
                for ci in range(len(_confs))
            ]
            speaker_summary = {
                names[k]: {
                    "votes": int((best_idx == k).sum()),
                    "mean_conf": round(float(confs[:, k].mean()), 4),
                    "max_conf": round(float(confs[:, k].max()), 4),
                }
                for k in range(len(names))
            }
            dbg_result = {
                "name": resolved_name, "match": is_match,
                # nearest-enrolled similarity (the winning vote), regardless of match
                "confidence": round(best_conf, 4),
                "threshold": self._match_threshold,
                "voiceprint_hash": vp_hash,
                "num_enrolled": len(known),
                "num_query_chunks": int(query_chunks.shape[0]),
                "embedding_dim": int(query_chunks.shape[1]),
                "enrolled_speakers": names,
                # EVERY enrolled speaker (incl. 0-vote losers): votes + mean/max sim.
                "speaker_summary": speaker_summary,
                # Each chunk vs every speaker + which one that chunk voted for.
                "per_chunk_scores": per_chunk_scores,
                # Vote-winners only — the actual decision breakdown.
                "candidates": [
                    {"name": n, "confidence": round(c, 4), "votes": v}
                    for n, c, v in scores
                ],
                "source_type": source_type,
                "duration_s": dur, "rms": rms,
                "preprocessing": self._debug_preproc_parts()[1],
                "unknown_audio_path": saved_path,
            }
            if is_match:
                # Enrolled → dir = <name>_<nearest-enrolled score>.
                dbg_cls, dbg_conf = resolved_name, best_conf
            else:
                # Stranger → dir = stranger-<N>_<cluster match score>, and record
                # the re-appearance detail alongside the nearest-enrolled miss.
                st = self._debug_stranger or {}
                st_score = st.get("score")
                dbg_cls = _debug_stranger_label(vp_hash)
                dbg_conf = st_score if st_score is not None else 0.0
                dbg_result.update({
                    "stranger_match_score": (round(st_score, 4) if st_score is not None else None),
                    "stranger_reappeared": st.get("reappeared"),
                    "closest_cluster": st.get("closest_label"),
                    "num_stranger_clusters": st.get("num_clusters"),
                })
            self._debug.record(
                "recognize", cls=dbg_cls, confidence=dbg_conf,
                wavs={"input.wav": wav_bytes, **self._debug_preproc_parts()[0]},
                arrays={
                    "input_chunks.npy": query_chunks,
                    "input_embedding.npy": _l2(query_chunks.mean(axis=0)),
                    # [chunks x speakers] raw cosine; columns = enrolled_speakers order.
                    "chunk_scores.npy": confs,
                },
                result=dbg_result,
                # Per-stage latency + RSS delta -> profile.json (TEN-VAD and
                # the STOI gate each get their own entry under `stages`).
                profile=self._debug_profile_dict(),
            )
        return result

    # ------------------------------------------------------------ public: get

    def get_meta(self, name: str) -> Optional[dict[str, Any]]:
        """Return the full enrollment meta for one user, or None if not enrolled.

        Mirrors the per-row shape of :meth:`list_registered` but skips the
        registry walk. Used for idempotent retries on the enroll route — when
        the caller passes paths that have already been consumed, we can return
        the existing meta instead of erroring out.
        """
        norm = _normalize_label(name)
        if not self._has_profile(norm):
            return None
        voice_meta = self._read_metadata(norm)
        shared_meta = self._read_shared_metadata(norm)
        tg_username = shared_meta.get(
            "telegram_username", voice_meta.get("telegram_username", "")
        )
        tg_id = shared_meta.get(
            "telegram_id", voice_meta.get("telegram_id", "")
        )
        return {
            "name": norm,
            "display_name": shared_meta.get("display_name")
            or voice_meta.get("display_name", norm),
            "telegram_username": tg_username,
            "telegram_id": tg_id,
            "has_telegram_identity": bool(tg_username or tg_id),
            "enrollment_sources": voice_meta.get("enrollment_sources", []),
            "last_enrollment_source": voice_meta.get(
                "last_enrollment_source", ""
            ),
            "embedding_dim": voice_meta.get("embedding_dim", 0),
            "enrolled_at": voice_meta.get("enrolled_at"),
            "updated_at": voice_meta.get("updated_at"),
            # Counts/file lists come from disk, not the JSON — see
            # _disk_sample_fields.
            **self._disk_sample_fields(norm),
        }

    # ----------------------------------------------------------- public: list

    def list_registered(self) -> list[dict[str, Any]]:
        """Return users who have a registered voice (embedding file exists).

        Backed by the registry file but cross-verified with on-disk state so
        stale registry rows are skipped. Telegram identity is read fresh from
        the shared ``metadata.json`` on every call so renames propagate.

        Each entry includes ``enrollment_sources`` (e.g. ``["mic"]``,
        ``["telegram"]`` or ``["mic", "telegram"]``) and
        ``has_telegram_identity`` — so the skill can tell whether a mic-only
        user still needs to be linked to a Telegram account for DM targeting.
        """
        reg = self._load_registry()
        out: list[dict[str, Any]] = []
        for norm in sorted(reg.keys()):
            if not self._has_profile(norm):
                continue
            voice_meta = self._read_metadata(norm)
            shared_meta = self._read_shared_metadata(norm)
            tg_username = shared_meta.get(
                "telegram_username", voice_meta.get("telegram_username", "")
            )
            tg_id = shared_meta.get(
                "telegram_id", voice_meta.get("telegram_id", "")
            )
            out.append(
                {
                    "name": norm,
                    "display_name": shared_meta.get("display_name")
                    or voice_meta.get("display_name", norm),
                    "telegram_username": tg_username,
                    "telegram_id": tg_id,
                    "has_telegram_identity": bool(tg_username or tg_id),
                    "enrollment_sources": voice_meta.get(
                        "enrollment_sources", []
                    ),
                    "last_enrollment_source": voice_meta.get(
                        "last_enrollment_source", ""
                    ),
                    "embedding_dim": voice_meta.get("embedding_dim", 0),
                    "enrolled_at": voice_meta.get("enrolled_at"),
                    "updated_at": voice_meta.get("updated_at"),
                    **self._disk_sample_fields(norm),
                }
            )
        return out

    # ------------------------------------ public: identity-focused methods

    def get_telegram_id(self, name: str) -> str | None:
        """Return ``telegram_id`` for a user, or ``None`` if not set.

        Mirrors :meth:`FaceRecognizer.get_telegram_id` so any skill wanting
        to DM a person after voice recognition can use a single lookup.
        """
        norm = _normalize_label(name)
        meta = self._read_shared_metadata(norm)
        val = meta.get("telegram_id") or ""
        return val or None

    def get_telegram_username(self, name: str) -> str | None:
        norm = _normalize_label(name)
        meta = self._read_shared_metadata(norm)
        val = meta.get("telegram_username") or ""
        return val or None

    def lookup_by_telegram_id(self, telegram_id: str) -> str | None:
        """Reverse-lookup: given a Telegram user ID, return the norm label.

        Useful when a Telegram turn arrives and the skill wants to decide
        whether the sender already has a voice profile before enrolling.
        """
        if not telegram_id:
            return None
        reg = self._load_registry()
        for norm, entry in reg.items():
            if entry.get("telegram_id") == telegram_id:
                return norm
        return None

    def update_identity(
        self,
        name: str,
        telegram_username: str = "",
        telegram_id: str = "",
    ) -> dict[str, Any]:
        """Attach / update Telegram identity on an existing voice profile.

        Use this when a user enrolled by mic first (no Telegram info) later
        introduces themselves from Telegram — we can link the two without
        re-uploading audio or recomputing the embedding.
        """
        norm = _normalize_label(name)
        user_dir = self._users_dir / norm
        if not self._has_profile(norm):
            raise SpeakerRecognizerError(
                f"no voice profile for '{norm}' — call enroll first"
            )
        shared = _merge_shared_metadata(
            user_dir,
            display_name=name.strip() or None,
            telegram_username=telegram_username or None,
            telegram_id=telegram_id or None,
        )
        # Refresh mirrored fields in voice metadata + registry. Hold the per-user
        # lock across read-modify-write so this can't clobber a concurrent enroll
        # or migration commit (which would otherwise revert embed_model_version
        # to this stale snapshot). Re-read inside the lock.
        with self._user_lock(norm):
            voice_meta = self._read_metadata(norm)
            voice_meta["telegram_username"] = shared.get("telegram_username", "")
            voice_meta["telegram_id"] = shared.get("telegram_id", "")
            voice_meta["has_telegram_identity"] = bool(
                shared.get("telegram_id") or shared.get("telegram_username")
            )
            voice_meta["display_name"] = shared.get(
                "display_name", voice_meta.get("display_name", norm)
            )
            now_iso = self._now_iso()
            voice_meta["updated_at"] = now_iso
            self._write_metadata(norm, voice_meta)
            self._update_registry(norm, voice_meta)
        logger.info(
            "Linked Telegram identity to '%s' (username=%s, id=%s)",
            norm, telegram_username, telegram_id,
        )
        return voice_meta

    def reset_all(self) -> int:
        """Delete every registered voice profile.

        Mirrors :meth:`FaceRecognizer.reset_enrolled`. Only the ``voice/``
        subdir of each user is removed — the shared ``metadata.json``
        (telegram identity) is preserved because face / mood / wellbeing
        still depend on it.
        """
        count = 0
        reg = self._load_registry()
        for norm in list(reg.keys()):
            if self.remove(norm):
                count += 1
        # Best-effort: walk disk too in case registry was stale.
        if self._users_dir.is_dir():
            for entry in self._users_dir.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                voice_dir = self._voice_dir(entry.name)
                if voice_dir.is_dir():
                    try:
                        shutil.rmtree(voice_dir)
                        count += 1
                    except OSError as e:
                        logger.warning("reset_all: failed to drop %s: %s", voice_dir, e)
        # Clear registry file.
        with self._mu:
            self._save_registry({})
        logger.info("reset_all: cleared %d voice profiles", count)
        return count

    # --------------------------------------------------------------- helpers

    def _save_incoming_audio(self, wav_bytes: bytes) -> str:
        """Save the incoming recognize() WAV to the unknown-audio dir.

        We always save — even on a match — so there is a record of what the
        device heard and a stable path skills can reuse for follow-up
        enrollment flows. Written BEFORE recognition runs, so the paths that
        never reach a match decision (gate reject, embedding-server error)
        still have one.

        This is a ROLLING log: :meth:`_roll_incoming_log` keeps the newest
        ``HAL_MAX_INCOMING_FILES`` and evicts oldest-first.
        """
        _UNKNOWN_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        fname = (
            f"incoming_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.wav"
        )
        fpath = _UNKNOWN_AUDIO_DIR / fname
        try:
            fpath.write_bytes(wav_bytes)
        except OSError as e:
            logger.warning("failed to save incoming audio: %s", e)
            return ""
        with self._roll_lock:
            self._incoming_count += 1
            over = self._incoming_count > _MAX_INCOMING_FILES
        if over:
            self._roll_incoming_log()
        return str(fpath)

    @staticmethod
    def _count_incoming() -> int:
        """Number of incoming_*.wav currently in the log root."""
        if not _UNKNOWN_AUDIO_DIR.is_dir():
            return 0
        try:
            return sum(1 for p in _UNKNOWN_AUDIO_DIR.glob("incoming_*.wav") if p.is_file())
        except OSError as e:
            logger.warning("incoming-audio log: cannot count dir: %s", e)
            return 0

    def _roll_incoming_log(self) -> None:
        """Trim the incoming log to _MAX_INCOMING_FILES, oldest evicted first.

        Only reached when the in-memory counter says we are over, so the
        directory listing here is not paid for on an ordinary turn. The listing
        also RESYNCS the counter, which makes any drift self-healing — a file
        removed out-of-band, or a move whose decrement was lost, costs at most
        one early listing before the count is exact again.

        Touches ``incoming_*.wav`` in the root only. Cluster sub-dirs belong to
        the stranger tracker and are bounded by cluster eviction.
        """
        if _MAX_INCOMING_FILES <= 0:
            return
        if not _UNKNOWN_AUDIO_DIR.is_dir():
            return
        try:
            files = [
                (p.stat().st_mtime, p)
                for p in _UNKNOWN_AUDIO_DIR.glob("incoming_*.wav")
                if p.is_file()
            ]
        except OSError as e:
            logger.warning("incoming-audio log: cannot list dir: %s", e)
            return

        with self._roll_lock:
            self._incoming_count = len(files)
        if len(files) <= _MAX_INCOMING_FILES:
            return

        # Newest first, so everything past the cap is the oldest tail.
        ordered = sorted(files, key=lambda t: t[0], reverse=True)
        removed = 0
        for _mt, p in ordered[_MAX_INCOMING_FILES:]:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                logger.warning("incoming-audio log: cannot remove %s: %s", p, e)
        with self._roll_lock:
            self._incoming_count = max(0, len(files) - removed)
        if removed:
            logger.info(
                "Incoming-audio log: evicted %d oldest clip(s), %d kept (cap=%d)",
                removed, self._incoming_count, _MAX_INCOMING_FILES,
            )

    def _move_to_cluster(self, saved_path: str, vp_hash: Optional[str]) -> str:
        """Move a saved WAV into a per-cluster sub-dir, return the new path.

        Called right after voiceprint_hash is assigned so later tools (web UI,
        diagnostic scripts) can list all audio for a given cluster via a
        single directory listing. No-op when hash is empty or file missing —
        known-speaker WAVs stay in the flat _UNKNOWN_AUDIO_DIR.
        """
        if not vp_hash or not saved_path:
            return saved_path
        src = Path(saved_path)
        if not src.exists():
            return saved_path
        try:
            cluster_dir = src.parent / vp_hash
            cluster_dir.mkdir(parents=True, exist_ok=True)
            dst = cluster_dir / src.name
            src.rename(dst)
            # It left the root, so it no longer counts against the log cap.
            # Skipping this would only cost an early (self-correcting) listing,
            # but on a device hearing mostly strangers that is every turn.
            with self._roll_lock:
                self._incoming_count = max(0, self._incoming_count - 1)
            # Bound the cluster we just wrote to. Only that one directory is
            # touched, so the work is proportional to its own size and settles
            # at cap+1 entries — no sweep across every cluster.
            self._prune_cluster_dir(vp_hash, keep=dst)
            return str(dst)
        except OSError as e:
            logger.warning(
                "move %s to cluster %s failed: %s", saved_path, vp_hash, e,
            )
            return saved_path

    # ------------------------------------------------- voice stranger clustering

    def _cluster_labels_in_order(self) -> list[str]:
        """Distinct cluster labels, oldest first. Caller holds _stranger_lock.

        A cluster now owns SEVERAL rows, so "oldest" is the label whose first
        row appears earliest — not simply the first row in the array.
        """
        if self._stranger_labels is None:
            return []
        seen: list[str] = []
        for lbl in self._stranger_labels:
            s = str(lbl)
            if s not in seen:
                seen.append(s)
        return seen

    def _cluster_rows(self, label: str) -> Optional[np.ndarray]:
        """Rows belonging to one cluster. Caller holds _stranger_lock."""
        if self._stranger_embeds is None or self._stranger_labels is None:
            return None
        mask = np.asarray(self._stranger_labels) == label
        if not np.any(mask):
            return None
        return self._stranger_embeds[mask]

    def _evict_oldest_clusters(self) -> list[str]:
        """Retire whole clusters once over _MAX_VOICE_STRANGERS. Holds the lock.

        Evicts by LABEL, not by row. Slicing rows (as the face tracker does,
        correctly, because each of its strangers owns exactly one row) would
        here delete a cluster's oldest samples while leaving the rest —
        silently shrinking clusters instead of retiring them.

        Returns the evicted labels so the caller can remove their on-disk dirs
        AFTER releasing the lock.
        """
        labels = self._cluster_labels_in_order()
        if len(labels) <= _MAX_VOICE_STRANGERS:
            return []
        drop = set(labels[: len(labels) - _MAX_VOICE_STRANGERS])
        keep_mask = np.array(
            [str(lbl) not in drop for lbl in self._stranger_labels], dtype=bool
        )
        self._stranger_embeds = self._stranger_embeds[keep_mask]
        self._stranger_labels = self._stranger_labels[keep_mask]
        logger.info(
            "Evicting %d oldest voice cluster(s): %s", len(drop), sorted(drop),
        )
        return sorted(drop)

    @staticmethod
    def _remove_cluster_dir(label: str) -> None:
        """Delete a cluster's on-disk audio. Never call under _stranger_lock.

        Eviction used to drop only the in-memory row, leaving the directory
        behind forever: nothing else pruned _UNKNOWN_AUDIO_DIR, so evicted
        clusters accumulated on disk AND kept showing up in GET /voice/strangers
        (which lists the filesystem) as clusters that could never match again.
        """
        cluster_dir = _UNKNOWN_AUDIO_DIR / label
        if not cluster_dir.is_dir():
            return
        try:
            shutil.rmtree(cluster_dir)
            logger.info("Removed evicted cluster dir %s", cluster_dir)
        except OSError as e:
            logger.warning("failed to remove cluster dir %s: %s", cluster_dir, e)

    def _prune_cluster_dir(self, label: str, keep: Optional[Path] = None) -> int:
        """Trim one cluster dir to _MAX_CLUSTER_FILES, oldest evicted first.

        These WAVs are what an agent enrols a stranger from, so the cap is a
        deliberate trade rather than pure housekeeping — see
        SPEAKER_MAX_CLUSTER_FILES. Recency is the only criterion: the clips have
        no embedding sidecars, so there is nothing to rank them by acoustically
        without re-embedding every file.

        ``keep`` is never evicted. Normally it is the newest and would survive
        anyway, but ``rename`` preserves mtime, so a clip that sat in the root
        across a clock change could sort old — and evicting the very clip whose
        path was just handed to the agent is the one outcome worth ruling out.

        Returns the number of files removed.
        """
        if _MAX_CLUSTER_FILES <= 0 or not label:
            return 0
        cluster_dir = _UNKNOWN_AUDIO_DIR / label
        if not cluster_dir.is_dir():
            return 0
        try:
            files = [
                (p.stat().st_mtime, p)
                for p in cluster_dir.glob("*.wav")
                if p.is_file()
            ]
        except OSError as e:
            logger.warning("cluster prune %s: cannot list dir: %s", label, e)
            return 0
        if len(files) <= _MAX_CLUSTER_FILES:
            return 0

        paths = [p for _mt, p in sorted(files, key=lambda t: t[0], reverse=True)]
        if keep is not None and keep in paths:
            paths.remove(keep)
            paths.insert(0, keep)

        removed = 0
        for p in paths[_MAX_CLUSTER_FILES:]:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError as e:
                logger.warning("cluster prune %s: cannot remove %s: %s", label, p, e)
        if removed:
            logger.info(
                "Cluster %s: evicted %d oldest clip(s), %d kept (cap=%d)",
                label, removed, len(files) - removed, _MAX_CLUSTER_FILES,
            )
        return removed

    def _reconcile_cluster_dirs(self) -> int:
        """Delete cluster dirs that no longer have any centroid row.

        Run once at startup to clear orphans left behind by the old
        row-eviction path. Deliberately skipped when the stranger state failed
        to load — with no labels in memory, every dir would look orphaned and
        we would wipe the lot.
        """
        with self._stranger_lock:
            if self._stranger_labels is None:
                return 0
            live = {str(lbl) for lbl in self._stranger_labels}
        if not _UNKNOWN_AUDIO_DIR.is_dir():
            return 0
        removed = 0
        for d in sorted(_UNKNOWN_AUDIO_DIR.iterdir()):
            if not d.is_dir() or not _VOICE_STRANGER_DIR_RE.match(d.name):
                continue
            if d.name in live:
                # Kept, but it may predate the file cap — trim it now rather
                # than waiting for this stranger to speak again, which for a
                # cluster nobody returns to would be never.
                self._prune_cluster_dir(d.name)
                continue
            try:
                shutil.rmtree(d)
                removed += 1
            except OSError as e:
                logger.warning("reconcile: failed to remove %s: %s", d, e)
        if removed:
            logger.info(
                "Reconciled unknown-audio dir: removed %d orphaned cluster(s)",
                removed,
            )
        return removed

    def _assign_voiceprint_hash(self, query_chunks: np.ndarray) -> str:
        """Return a stable voice_<N> label for an unknown voice.

        Pools the per-chunk query embeddings into one L2-normalized vector and
        compares it against the stored cluster rows. A cluster matches when its
        BEST row scores >= self._match_threshold (raw cosine, the same bar an
        enrolled user must clear); otherwise a new cluster is allocated.

        A matching utterance is APPENDED to that cluster as another row rather
        than folded into an average, subject to the same diversity gate and cap
        the extended tier uses. The old code stored one centroid per cluster
        and never updated it, so a cluster with eight clips was still being
        matched against the embedding of clip #1 forever.

        Consumers don't call this directly — recognize() stamps the hash into
        its response when the speaker is unknown.
        """
        if query_chunks is None or len(query_chunks) == 0:
            return ""
        agg = query_chunks.mean(axis=0)
        norm = float(np.linalg.norm(agg))
        if norm == 0.0:
            return ""
        agg = (agg / norm).astype(np.float32)

        evicted: list[str] = []
        with self._stranger_lock:
            # Model-change guard
            self._ensure_stranger_store_compatible(int(agg.shape[0]))
            best_sim_pre = None  # captured for the "new cluster" log path
            best_label_pre: Optional[str] = None
            breakdown_pre = ""
            if self._stranger_embeds is not None and len(self._stranger_embeds) > 0:
                # Both sides L2-normalized -> the dot product IS raw cosine,
                # compared directly against the threshold. No rescaling.
                row_sims = self._stranger_embeds @ agg
                labels_in_order = self._cluster_labels_in_order()
                label_arr = np.asarray(self._stranger_labels)
                # Per-cluster score = its best row, mirroring how an enrolled
                # speaker is scored across their bank.
                cluster_sims = {
                    lbl: float(row_sims[label_arr == lbl].max())
                    for lbl in labels_in_order
                }
                best_label_pre = max(cluster_sims, key=lambda k: cluster_sims[k])
                best_sim_pre = cluster_sims[best_label_pre]
                breakdown_pre = ", ".join(
                    f"{lbl}={cluster_sims[lbl]:.3f}" for lbl in labels_in_order
                )
                if best_sim_pre >= self._match_threshold:
                    logger.info(
                        "Voiceprint hash: %s (matched existing cluster, "
                        "cos=%.3f, threshold=%.3f) | scores=[%s]",
                        best_label_pre, best_sim_pre,
                        self._match_threshold, breakdown_pre,
                    )
                    self._append_cluster_row(best_label_pre, agg)
                    self._save_strangers()
                    if self._debug.enabled:  # SPEAKER-DEBUG
                        self._debug_stranger = {
                            "reappeared": True, "score": best_sim_pre,
                            "closest_label": best_label_pre,
                            "num_clusters": len(self._cluster_labels_in_order()),
                        }
                    return best_label_pre

            # No match — allocate a new cluster.
            self._stranger_counter = (self._stranger_counter + 1) % int(1e6)
            label = f"{_VOICE_STRANGER_PREFIX}{self._stranger_counter}"
            new_row = agg.reshape(1, -1).astype(np.float32)
            new_lbl = np.array([label])
            if self._stranger_embeds is None:
                self._stranger_embeds = new_row
                self._stranger_labels = new_lbl
            else:
                self._stranger_embeds = np.concatenate(
                    [self._stranger_embeds, new_row], axis=0,
                )
                self._stranger_labels = np.concatenate(
                    [self._stranger_labels, new_lbl], axis=0,
                )

            evicted = self._evict_oldest_clusters()
            self._save_strangers()
            num_clusters = len(self._cluster_labels_in_order())
            if best_sim_pre is not None:
                # Hit the "no existing cluster matched" branch — surface the
                # closest miss so operators can spot threshold edge cases.
                logger.info(
                    "Voiceprint hash: %s (new cluster, total=%d) | "
                    "closest=%s cos=%.3f below threshold=%.3f | scores=[%s]",
                    label, num_clusters,
                    best_label_pre, best_sim_pre,
                    self._match_threshold, breakdown_pre,
                )
            else:
                logger.info(
                    "Voiceprint hash: %s (new cluster, total=%d) | "
                    "no prior clusters",
                    label, num_clusters,
                )
            if self._debug.enabled:  # SPEAKER-DEBUG
                self._debug_stranger = {
                    "reappeared": False, "score": best_sim_pre,
                    "closest_label": best_label_pre,
                    "num_clusters": num_clusters,
                }

        # Disk work happens with the lock released.
        for lbl in evicted:
            self._remove_cluster_dir(lbl)
        return label

    def _append_cluster_row(self, label: str, row: np.ndarray) -> None:
        """Add one row to a cluster, diversity-gated and capped. Holds the lock.

        Same shape as the extended tier: a near-duplicate of a row we already
        hold adds nothing but false-accept surface, and the cap bounds how far
        max-over-rows can inflate this cluster's score.
        """
        rows = self._cluster_rows(label)
        if rows is not None and len(rows):
            if float(np.max(rows @ row)) > _DIVERSITY_COS:
                return  # redundant with a row we already hold
            if len(rows) >= _MAX_CLUSTER_SAMPLES:
                return  # cluster is full; existing rows already span it
        self._stranger_embeds = np.concatenate(
            [self._stranger_embeds, row.reshape(1, -1).astype(np.float32)], axis=0,
        )
        self._stranger_labels = np.concatenate(
            [self._stranger_labels, np.array([label])], axis=0,
        )

    def _match_stranger_clusters(
        self, query_rows: np.ndarray, threshold: float,
    ) -> list[str]:
        """Cluster labels whose best row scores >= ``threshold`` against any query row.

        Used by enroll() to claim clusters that belong to the person being
        enrolled. ``threshold`` is RAW cosine and callers pass _MATCH_COS —
        there is no longer a looser merge gate. The old one admitted clips at
        0.625 scaled that were then used to judge the user's own enrollment
        audio at 0.75 scaled, which is how a stray 1s chat clip could end up
        replacing a deliberate 15s recording.
        """
        q = np.atleast_2d(np.asarray(query_rows, dtype=np.float32))
        q = np.stack([_l2(r) for r in q], axis=0)
        with self._stranger_lock:
            # Same model-change guard as the recognize path (see
            # _assign_voiceprint_hash) — never compare against centroids from a
            # superseded model.
            self._ensure_stranger_store_compatible(int(q.shape[0]))
            if (
                self._stranger_embeds is None
                or self._stranger_labels is None
                or len(self._stranger_embeds) == 0
            ):
                logger.info("Cluster claim scan: no stranger rows to compare")
                return []
            # [rows, queries] raw cosine -> best query per row -> best row per cluster.
            row_sims = (self._stranger_embeds @ q.T).max(axis=1)
            label_arr = np.asarray(self._stranger_labels)
            labels_in_order = self._cluster_labels_in_order()
            cluster_sims = {
                lbl: float(row_sims[label_arr == lbl].max())
                for lbl in labels_in_order
            }
            breakdown = ", ".join(
                f"{lbl}={cluster_sims[lbl]:.3f}" for lbl in labels_in_order
            )
            logger.info(
                "Cluster claim scan: threshold=%.2f query=[%s]", threshold, breakdown,
            )
            return [
                lbl for lbl in labels_in_order if cluster_sims[lbl] >= threshold
            ]

    def _ensure_stranger_store_compatible(self, query_dim: int) -> None:
        """Wipe the stranger store unless it is PROVEN to match the current model.

        Caller must hold ``_stranger_lock``. The verdict is deliberately
        conservative — the store is kept ONLY when we can prove it came from the
        model in use right now:

        * ``_server_model_version`` (the live model, refreshed by every /embed
          call) is known, the store's stamp **equals** it, AND the stored
          embedding dim equals the query dim → **keep**.
        * Anything else — a **missing** stamp, a **different** stamp, or a
          **different** dim — cannot prove same-model provenance, so the store
          is **wiped**. We never ASSUME an unstamped store is current: a
          same-dim checkpoint swap under a different model would otherwise slip
          through (exactly the case a dim check alone misses).

        When the server reports **no** version at all we have no version signal,
        so we fall back to a dim-only guard (a same-dim swap is undetectable
        without a version). Strangers are anonymous / ephemeral, so a mismatch
        WIPES the table (never re-embeds); ``_stranger_counter`` stays monotonic
        so a fresh ``voice_<N>`` never collides with a leftover cluster dir.
        """
        cur = self._server_model_version
        if self._stranger_embeds is None or len(self._stranger_embeds) == 0:
            # Empty store — nothing to invalidate; adopt the current stamp so a
            # fresh store is labelled with the model it will be built under.
            if cur and self._stranger_model_version != cur:
                self._stranger_model_version = cur
                self._save_strangers()
            return

        dim_ok = int(self._stranger_embeds.shape[1]) == int(query_dim)
        version_ok = (not cur) or (self._stranger_model_version == cur)
        if version_ok and dim_ok:
            return
        reason = (
            f"stranger store not confirmed for current model "
            f"(store={self._stranger_model_version or '<unset>'}/"
            f"dim{int(self._stranger_embeds.shape[1])} -> "
            f"server={cur or '<unset>'}/dim{int(query_dim)})"
        )
        self._stranger_model_version = cur
        self._wipe_stranger_store(reason)

    def _wipe_stranger_store(self, reason: str) -> None:
        """Drop all stranger data: in-memory centroids + on-disk WAV clusters.

        Caller must hold ``_stranger_lock``. ``_stranger_counter`` is KEPT
        (monotonic) so a new ``voice_<N>`` label can never collide with any
        leftover cluster dir. Persists the wipe so a restart does not reload the
        superseded centroids.
        """
        n = 0 if self._stranger_embeds is None else len(self._stranger_embeds)
        logger.warning(
            "Wiping voice stranger store (%d cluster(s), counter kept at %d) — %s",
            n, self._stranger_counter, reason,
        )
        self._stranger_embeds = None
        self._stranger_labels = None
        # Remove EVERY on-disk voice_<N>/ dir — including orphans whose centroid
        # was already evicted. Scan the disk (not the label table) so nothing is
        # stranded; _remove_cluster_dirs applies the same regex filter, so any
        # non-cluster entry is skipped.
        try:
            names = [e.name for e in _UNKNOWN_AUDIO_DIR.iterdir() if e.is_dir()]
        except OSError as e:
            logger.warning("Wipe strangers: failed to list cluster dirs: %s", e)
            names = []
        self._remove_cluster_dirs(names)
        self._save_strangers()

    def _remove_cluster_dirs(self, labels: Iterable[str]) -> None:
        """rmtree the on-disk ``voice_<N>/`` WAV dirs for the given labels.

        The single place that deletes cluster folders. Used by eviction (the
        specific labels pushed out of the centroid table) and by
        ``_wipe_stranger_store`` (every on-disk dir). Non-cluster names are
        skipped via the ``voice_<N>`` regex. Best-effort; never raises. Caller
        holds ``_stranger_lock``.
        """
        for label in labels:
            if not label or not _VOICE_STRANGER_DIR_RE.match(label):
                continue
            cluster_dir = _UNKNOWN_AUDIO_DIR / label
            if cluster_dir.is_dir():
                shutil.rmtree(cluster_dir, ignore_errors=True)

    def _save_strangers(self) -> None:
        """Persist stranger state to disk. Caller must hold _stranger_lock.

        Always persists the counter + model-version stamp. Centroid tables are
        written when present and DELETED when the store was wiped (embeds None),
        so a restart never reloads centroids a model change invalidated.
        """
        embeds_path = _VOICE_STRANGERS_DIR / "embeds.npy"
        labels_path = _VOICE_STRANGERS_DIR / "labels.npy"
        try:
            if self._stranger_embeds is not None and self._stranger_labels is not None:
                np.save(embeds_path, self._stranger_embeds)
                np.save(labels_path, self._stranger_labels)
            else:
                embeds_path.unlink(missing_ok=True)
                labels_path.unlink(missing_ok=True)
            np.save(
                _VOICE_STRANGERS_DIR / "counter.npy",
                np.array(self._stranger_counter),
            )
            (_VOICE_STRANGERS_DIR / "version.txt").write_text(
                self._stranger_model_version or ""
            )
        except OSError as e:
            logger.warning("save voice strangers failed: %s", e)

    def _load_strangers(self) -> None:
        """Load stranger state from disk on startup. Silent on missing files."""
        embeds_path = _VOICE_STRANGERS_DIR / "embeds.npy"
        labels_path = _VOICE_STRANGERS_DIR / "labels.npy"
        counter_path = _VOICE_STRANGERS_DIR / "counter.npy"
        version_path = _VOICE_STRANGERS_DIR / "version.txt"
        # Counter + version stamp survive a wipe (centroids may be absent) — load
        # them first so a monotonic counter never reuses an old label.
        if counter_path.exists():
            try:
                self._stranger_counter = int(np.load(counter_path))
            except Exception:
                self._stranger_counter = 0
        if version_path.exists():
            try:
                self._stranger_model_version = version_path.read_text().strip() or None
            except OSError:
                self._stranger_model_version = None
        if not (embeds_path.exists() and labels_path.exists()):
            return
        try:
            self._stranger_embeds = np.load(embeds_path)
            self._stranger_labels = np.load(labels_path)
            logger.info(
                "Loaded %d voice strangers (counter=%d, model=%s)",
                len(self._stranger_embeds), self._stranger_counter,
                self._stranger_model_version or "<unset>",
            )
        except Exception as e:
            logger.warning("load voice strangers failed: %s", e)
            self._stranger_embeds = None
            self._stranger_labels = None
