"""Speaker voice recognition service.

Stores per-user voice embeddings under ``/root/local/users/<name>/voice/`` and
recognizes speakers via cosine similarity. Embeddings are computed by a
configurable external API (see ``SPEAKER_EMBEDDING_API_URL``).

External API contract:
    POST {SPEAKER_EMBEDDING_API_URL}
    Headers: X-API-Key: {SPEAKER_EMBEDDING_API_KEY} (optional)
    Body:    {"audios_b64": ["<base64 WAV>", ...]}
    Response: {"embedding": [float, float, ...]}  (single 1-D vector
              aggregated from all inputs, any dimension)

Storage layout per user::

    /root/local/users/<norm>/
        metadata.json           — SHARED identity (telegram_username, telegram_id,
                                   display_name). Same file face-enroll writes —
                                   merged on write, never overwritten blindly.
        voice/
            embedding.npy       — single L2-normalized aggregated vector [D].
                                   Mirrors dlbackend's per-speaker storage so
                                   recognize uses the same per-chunk voting
                                   logic as dlbackend's /recognize endpoint.
            metadata.json       — voice-specific (enrolled_at, updated_at,
                                   num_samples, sample_files, embedding_dim)
            sample_<ts>_<uuid>.wav  — source WAV files (16kHz mono)

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

from lelamp import config
from lelamp.service.sensing.crypto import CryptoSession, resolve_public_key

logger = logging.getLogger("lelamp.voice.speaker")

# --- Storage layout (paths come from lelamp.config) ---
_USERS_DIR = Path(config.USERS_DIR)
_VOICE_SUBDIR = "voice"
_EMBEDDING_FILE = "embedding.npy"
_METADATA_FILE = "metadata.json"
_REGISTRY_FILE = _USERS_DIR / ".voice_registry.json"
_UNKNOWN_AUDIO_DIR = Path(config.SPEAKER_UNKNOWN_AUDIO_DIR)

# --- External embedding API (centralized in lelamp.config) ---
_API_URL = config.SPEAKER_EMBEDDING_API_URL
_API_KEY = config.SPEAKER_EMBEDDING_API_KEY
_API_TIMEOUT_S = config.SPEAKER_EMBEDDING_API_TIMEOUT_S
_MATCH_THRESHOLD = config.SPEAKER_MATCH_THRESHOLD
_ENROLL_CONSISTENCY_THRESHOLD = config.SPEAKER_ENROLL_CONSISTENCY_THRESHOLD

# --- Voice stranger clustering ---
# Assigns a stable "voiceprint_hash" (voice_<N> label) to every unknown voice
# so callers can track "same unknown speaker seen multiple times" without
# needing voiceprint_hash support from the embedding backend. Mirrors the
# face stranger tracker in facerecognizer.py.
_VOICE_STRANGERS_DIR = Path(
    os.environ.get("LELAMP_VOICE_STRANGERS_DIR", "/root/local/voice_strangers")
)
# All thresholds in this file use SCALED cosine in [0, 1] (`(raw + 1) / 2`).
# That includes MATCH / CONSISTENCY plus the stranger thresholds below — call
# sites convert the raw `embeds @ query` dot-product to scaled before
# comparing. Ordering: CLUSTER_MERGE < STRANGER_MATCH < MATCH = CONSISTENCY,
# i.e. the merge gate (used at enroll time to pull fragmented clusters back
# together) is the loosest, stranger-grouping sits between, and the
# recognize/match decision is strictest.
#
# ECAPA-TDNN same-speaker raw cosine on VoxCeleb clusters around 0.3–0.5
# (EER ≈ 0.3–0.4) → scaled ~0.65–0.75. Stranger defaults sit just below the
# EER band on purpose — false grouping at loose thresholds is bounded by the
# Step 5 consistency filter in enroll() and the MATCH gate in recognize().
_VOICE_STRANGER_MATCH_THRESHOLD = float(
    os.environ.get("LELAMP_VOICE_STRANGER_MATCH_THRESHOLD", "0.675")
)
# Cap cluster count so disk doesn't grow unbounded. Oldest evicted first.
_MAX_VOICE_STRANGERS = int(
    os.environ.get("LELAMP_MAX_VOICE_STRANGERS", "50")
)
_VOICE_STRANGER_PREFIX = "voice_"
_VOICE_STRANGER_DIR_RE = re.compile(r"^voice_\d+$")

# Target sample rate for stored/enrolled audio (matches STT pipeline).
_TARGET_SR = 16000

# Chunk window the /embed endpoint slices the waveform with before per-chunk
# embedding extraction. Bumped from the dlbackend default (0.5s) because
# smart-lamp audio is overwhelmingly single-speaker per turn — longer chunks
# yield smoother per-chunk embeddings, at the cost of fewer votes in
# recognize() and reduced ability to detect a speaker switch mid-turn.
# Audio shorter than this collapses to a single chunk (voting degenerates to
# plain 1-vs-1 cosine match).
_CHUNK_SECONDS = float(os.environ.get("LELAMP_SPEAKER_CHUNK_SECONDS", "3.0"))


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
    """Compute raw cosine similarity in range [-1, 1].

    Tolerates non-normalized inputs (unlike plain ``np.dot`` which requires
    pre-normalized vectors). The ``+ 1e-12`` guards against zero-norm inputs.
    Returns the confidence in range [0, 1].
    """
    raw_cos = float(np.dot(e1, e2) / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-12))
    return (raw_cos + 1.0) / 2.0


def _l2(vec: np.ndarray) -> np.ndarray:
    """L2-normalize; return unchanged if norm is ~0."""
    arr = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if n < 1e-12:
        return arr
    return (arr / n).astype(np.float32)


def _weighted_aggregate(
    embeddings: list[np.ndarray], *, power: float = 4.0
) -> np.ndarray:
    """Self-consistency weighted mean + L2-normalize.

    Mirrors ``dlbackend.audio_preprocess.weighted_aggregate`` so pooling
    per-sample embeddings client-side produces a vector comparable with one
    the server would produce from the same samples — without the server-side
    artifact of concatenating multiple WAVs into a single waveform before
    VAD / chunking (which the /embed endpoint currently does).

    Each embedding is weighted by its cosine sim to the L2-normalized
    median centroid, mapped to [0, 1] and raised to ``power`` to sharpen —
    outlier samples (a rejected-but-still-on-disk recording, for example)
    contribute near zero to the final vector.
    """
    if not embeddings:
        raise SpeakerRecognizerError("no embeddings to aggregate")
    stack = np.stack([_l2(e) for e in embeddings], axis=0).astype(np.float32)
    centroid = _l2(np.median(stack, axis=0))
    sims = stack @ centroid
    sims01 = np.clip((sims + 1.0) / 2.0, 0.0, 1.0)
    weights = sims01.astype(np.float32) ** float(power)
    total = float(weights.sum())
    if total < 1e-9:
        weights = np.full(stack.shape[0], 1.0 / stack.shape[0], dtype=np.float32)
    else:
        weights = (weights / total).astype(np.float32)
    agg = (stack * weights[:, None]).sum(axis=0)
    return _l2(agg)

def _sample_origin(filename: str) -> str:
    """Parse the origin tag encoded in ``sample_<origin>_<ts>_<uuid>.wav``.

    Legacy files ``sample_<ts>_<uuid>.wav`` (no origin) → ``"unknown"``.
    """
    parts = filename.split("_", 2)
    if len(parts) >= 2 and parts[0] == "sample":
        candidate = parts[1]
        if candidate in ("mic", "telegram", "other"):
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


def _float32_waveform_to_wav_bytes(waveform: np.ndarray) -> bytes:
    """Encode a float32 mono waveform into 16kHz PCM_16 WAV bytes."""
    try:
        import soundfile as sf  # type: ignore
    except ImportError as e:
        raise SpeakerRecognizerError(
            "soundfile is required for WAV processing"
        ) from e
    buf = io.BytesIO()
    sf.write(buf, np.asarray(waveform, dtype=np.float32), _TARGET_SR, format="WAV", subtype="PCM_16")
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
        self._users_dir = Path(users_dir) if users_dir else _USERS_DIR
        self._match_threshold = (
            match_threshold if match_threshold is not None else _MATCH_THRESHOLD
        )
        self._mu = threading.Lock()

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
        _VOICE_STRANGERS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_strangers()

        logger.info(
            "SpeakerRecognizer ready (api=%s, threshold=%.2f, users_dir=%s, strangers=%d)",
            self._api_url or "<unset>",
            self._match_threshold,
            self._users_dir,
            0 if self._stranger_embeds is None else len(self._stranger_embeds),
        )

    @property
    def available(self) -> bool:
        return bool(self._api_url)

    # ------------------------------------------------------------------ paths

    def _voice_dir(self, norm: str) -> Path:
        return self._users_dir / norm / _VOICE_SUBDIR

    def _embedding_path(self, norm: str) -> Path:
        return self._voice_dir(norm) / _EMBEDDING_FILE

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
            _REGISTRY_FILE.write_text(json.dumps(registry, indent=2))
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
                "embedding_dim": meta.get("embedding_dim", 0),
            }
            self._save_registry(reg)

    def _remove_from_registry(self, norm: str) -> None:
        with self._mu:
            reg = self._load_registry()
            if norm in reg:
                del reg[norm]
                self._save_registry(reg)

    # -------------------------------------------------------------- external

    def _call_embedding_api(
        self, audios_b64: list[str], *, return_chunks: bool = False
    ) -> np.ndarray:
        """POST audios to the embedding API.

        Returns the L2-normalized aggregated vector ``[D]`` by default. When
        ``return_chunks=True`` returns the matrix of per-chunk embeddings
        ``[M, D]`` instead — used by ``recognize()`` to do per-chunk voting
        against stored speakers (mirroring dlbackend's /recognize logic).
        """
        if not self._api_url:
            raise SpeakerRecognizerError(
                "SPEAKER_EMBEDDING_API_URL not configured"
            )
        if not audios_b64:
            raise SpeakerRecognizerError("no audio to embed")

        logger.info(
            "Calling embedding API with %d audios at %s (return_chunks=%s)",
            len(audios_b64), self._api_url, return_chunks,
        )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        body: dict[str, Any] = {
            "audios_b64": audios_b64,
            "chunk_seconds": _CHUNK_SECONDS,
        }
        if return_chunks:
            body["return_chunks"] = True

        try:
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

        try:
            if self._crypto is not None:
                payload = json.loads(self._crypto.unwrap_http_response(resp.content))
            else:
                payload = resp.json()
        except ValueError as e:
            raise EmbeddingAPIUnavailableError(
                f"embedding API returned non-JSON: {e}"
            ) from e

        if return_chunks:
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
        """Validate + wrap a single WAV as a one-element base64 list.

        The ``/embed`` endpoint accepts a list of audios but concatenates
        them into one waveform before preprocessing/VAD/chunking, so
        pre-splitting long audio client-side would only add a lossy
        float32 → PCM_16 round-trip per slice. Pass the whole WAV through
        and let the server window it with its own chunk_with_stride.

        Two gates before we hit the network:

        * **Duration ≥ 0.5s.** The server's windowing uses
          ``min_chunk_sec ≈ 0.4s`` *after* VAD trims silence, so leaving a
          100 ms headroom on the raw audio prevents the server from
          silently returning "no embeddings produced".
        * **RMS ≥ 1e-4 (~−80 dBFS).** A silent file would also pass 0.5s;
          we check loudness on the raw signal to reject it early, even
          though VAD might have caught it server-side. This also dodges
          ONNX crashes (``Invalid input shape: {80, 0}``) when kaldi fbank
          produces an empty feature tensor.
        """
        waveform = _wav_bytes_to_float32_16k_mono(wav_bytes)

        min_samples = int(0.5 * _TARGET_SR)
        if waveform.shape[0] < min_samples:
            raise SpeakerRecognizerError(
                f"audio too short for embedding: {waveform.shape[0]/_TARGET_SR:.2f}s < 0.5s"
            )

        rms = float(np.sqrt(np.mean(waveform.astype(np.float64) ** 2)))
        if rms < 1e-4:
            raise SpeakerRecognizerError(
                f"audio too silent for embedding: rms={rms:.6f} < 1e-4"
            )

        return [base64.b64encode(_float32_waveform_to_wav_bytes(waveform)).decode("ascii")]

    # ------------------------------------------------------------- metadata

    def _read_metadata(self, norm: str) -> dict[str, Any]:
        p = self._metadata_path(norm)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

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

    def _load_all_embeddings(self) -> dict[str, np.ndarray]:
        """Load every stored aggregated embedding [D] — source of truth for recognize().

        Mirrors dlbackend's per-speaker storage: one L2-normalized vector
        per user. Recognize then runs per-chunk voting against these.
        """
        out: dict[str, np.ndarray] = {}
        if not self._users_dir.is_dir():
            return out
        for entry in sorted(self._users_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            emb_path = self._voice_dir(entry.name) / _EMBEDDING_FILE
            if not emb_path.is_file():
                continue
            try:
                vec = np.load(emb_path).astype(np.float32)
                if vec.ndim != 1 or vec.size == 0:
                    continue
                n = float(np.linalg.norm(vec))
                if n < 1e-12:
                    continue
                out[entry.name] = (vec / n).astype(np.float32)
            except Exception as e:
                logger.warning("failed to load embedding for %s: %s", entry.name, e)
        return out

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

        New sample WAVs are appended to the user's ``voice/`` folder and the
        embedding is (re)computed from ALL samples in the folder, producing a
        single aggregated representative vector.

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
        origin = origin if origin in ("mic", "telegram", "other") else "other"

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
        # Strict policy: the voice/ folder ONLY contains audios that
        # contributed to the final embedding. So we do all the work in
        # memory first — validate, embed, filter — and ONLY THEN commit
        # surviving samples to disk. Any audio that fails validation or
        # the consistency filter never touches the folder.
        # ------------------------------------------------------------

        # Step 1 — Decode + normalize incoming audios (in-memory only).
        new_wavs: list[bytes] = []
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
        for idx, wb in enumerate(new_wavs):
            try:
                payload = self._prepare_wav_for_embedding(wb)
                emb = self._call_embedding_api(payload)
                new_embeddings.append((wb, emb))
            except EmbeddingAPIUnavailableError:
                raise
            except SpeakerRecognizerError as e:
                per_sample_errors.append((idx, str(e)))
                logger.warning(
                    "Enroll: rejected new sample #%d — %s (not saved to disk)",
                    idx, e,
                )

        if not new_embeddings:
            # Surface the actual reason from dlbackend (VAD reject text, etc.)
            # or from local gates (too short / silent) — no hardcoded summary.
            if len(per_sample_errors) == 1:
                raise SpeakerRecognizerError(per_sample_errors[0][1])
            details = "; ".join(
                f"sample #{i}: {msg}" for i, msg in per_sample_errors
            )
            raise SpeakerRecognizerError(f"no valid new samples — {details}")

        # Step 2b — Pull all WAVs from clusters that should contribute samples
        # to this enrollment. Two paths into the cluster set, both unioned:
        #   (a) Explicit claim — any source path whose parent dir is a
        #       ``voice_<N>`` cluster. The caller (LLM via OpenClaw skill)
        #       may pass only one path from a cluster even when the cluster
        #       has more sibling WAVs; we treat passing any path as claiming
        #       the WHOLE cluster, so no audio gets stranded if the LLM
        #       happens to surface only one sample per turn.
        #   (b) Centroid match — any cluster whose stored centroid sits within
        #       merge_threshold of the query mean. Captures distance / volume
        #       drift where the same person got fragmented across voice_<N>
        #       clusters with low pairwise centroid similarity, but each one
        #       still aligns with the new enrollment audio.
        # Filepath sources only — base64 has no path to resolve. Samples
        # pulled in go through the same consistency filter (Step 5) as any
        # other sample, so false matches get their outliers dropped, and
        # _drop_consumed_clusters at the tail cleans up every consumed
        # cluster regardless of how many samples survived.
        if source_type == "filepath" and new_embeddings:
            # SCALED cosine in [0, 1] — same unit as STRANGER_MATCH and
            # MATCH. 0.625 sits below the EER band so same-person-
            # different-distance gets absorbed; the Step 5 consistency
            # filter below catches false merges from loose matches.
            merge_threshold = float(
                os.environ.get("LELAMP_CLUSTER_MERGE_THRESHOLD", "0.625")
            )

            # (a) Path-based claim — collect cluster names from sources.
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
                    parent_name = resolved.parent.name
                    if _VOICE_STRANGER_DIR_RE.match(parent_name):
                        claimed_hashes.add(parent_name)

            # (b) Centroid-based match.
            query_mean = _weighted_aggregate(
                [emb for _wb, emb in new_embeddings]
            )
            matched_hashes = set(self._match_stranger_clusters(
                query_mean, merge_threshold,
            ))

            consume_hashes = sorted(claimed_hashes | matched_hashes)
            if claimed_hashes:
                logger.info(
                    "Cluster claim: source paths claimed %d cluster(s) %s",
                    len(claimed_hashes), sorted(claimed_hashes),
                )

            merged_added = 0
            for h in consume_hashes:
                cluster_dir_path = _UNKNOWN_AUDIO_DIR / h
                if not cluster_dir_path.is_dir():
                    continue
                for wav in sorted(cluster_dir_path.glob("*.wav")):
                    wav_str = str(wav)
                    if wav_str in sources:
                        continue  # caller already passed this path in
                    try:
                        raw = _read_bytes(wav_str)
                    except Exception as e:
                        logger.warning(
                            "Cluster pull: cannot read %s — %s", wav_str, e,
                        )
                        continue
                    try:
                        wb = _ensure_wav_16k_mono(raw)
                        payload = self._prepare_wav_for_embedding(wb)
                        emb = self._call_embedding_api(payload)
                    except EmbeddingAPIUnavailableError:
                        raise
                    except SpeakerRecognizerError as e:
                        logger.info(
                            "Cluster pull: skip %s — %s", wav.name, e,
                        )
                        continue
                    new_wavs.append(wb)
                    new_embeddings.append((wb, emb))
                    sources.append(wav_str)
                    merged_added += 1
            if consume_hashes:
                logger.info(
                    "Cluster pull: %d WAV(s) from %d cluster(s) %s "
                    "(claimed=%d, centroid-matched=%d, threshold=%.2f)",
                    merged_added, len(consume_hashes), consume_hashes,
                    len(claimed_hashes), len(matched_hashes - claimed_hashes),
                    merge_threshold,
                )

        # Step 3 — Load EXISTING samples on disk + compute their embeddings.
        # Two failure modes, handled separately:
        #   a) _prepare_wav_for_embedding fails → the WAV file itself is
        #      corrupt / silent / too short. Safe to delete so the folder
        #      doesn't carry a permanently broken sample.
        #   b) _call_embedding_api fails → the server rejected or is down.
        #      NEVER delete: the file was previously accepted and may be
        #      fine once the API recovers. EmbeddingAPIUnavailableError
        #      also aborts the whole enroll so we don't proceed with a
        #      partial view of existing samples.
        existing_on_disk = sorted(voice_dir.glob("sample_*.wav"))
        existing_embs: dict[Path, np.ndarray] = {}
        for p in existing_on_disk:
            try:
                payload = self._prepare_wav_for_embedding(p.read_bytes())
            except SpeakerRecognizerError as e:
                logger.warning(
                    "Enroll: removing broken existing sample %s — %s",
                    p.name, e,
                )
                try:
                    p.unlink()
                except OSError as ose:
                    logger.warning("Enroll: failed to delete %s: %s", p, ose)
                continue
            try:
                existing_embs[p] = self._call_embedding_api(payload)
            except EmbeddingAPIUnavailableError:
                raise
            except SpeakerRecognizerError as e:
                logger.warning(
                    "Enroll: skipping existing sample %s — server rejected (%s), file kept",
                    p.name, e,
                )

        # Step 4 — Reference = the LATEST NEW wav (user's most recent intent
        # wins). All other samples (new + existing) are scored against it.
        ref_wb, ref_emb = new_embeddings[-1]

        kept_new: list[tuple[bytes, np.ndarray]] = [(ref_wb, ref_emb)]
        dropped_new = 0
        for wb, emb in new_embeddings[:-1]:
            sim = _cosine_similarity(emb, ref_emb)
            if sim >= _ENROLL_CONSISTENCY_THRESHOLD:
                kept_new.append((wb, emb))
            else:
                dropped_new += 1
                logger.info(
                    "Enroll: dropped new sample (sim=%.2f < %.2f) — not written to disk",
                    sim, _ENROLL_CONSISTENCY_THRESHOLD,
                )

        kept_existing: list[tuple[Path, np.ndarray]] = []
        dropped_existing: list[tuple[Path, float]] = []
        for p, emb in existing_embs.items():
            sim = _cosine_similarity(emb, ref_emb)
            if sim >= _ENROLL_CONSISTENCY_THRESHOLD:
                kept_existing.append((p, emb))
            else:
                dropped_existing.append((p, sim))

        # Step 5 — Delete dropped EXISTING samples from disk.
        for p, sim in dropped_existing:
            try:
                p.unlink()
                logger.info(
                    "Enroll: removed stale existing sample %s (sim=%.2f < %.2f)",
                    p.name, sim, _ENROLL_CONSISTENCY_THRESHOLD,
                )
            except OSError as e:
                logger.warning("Enroll: failed to delete %s: %s", p, e)

        # Step 6 — Commit kept NEW wavs to disk. Stable millisecond prefix
        # ensures lex sort = chronological order for later reference picks.
        # Tiny sleep between writes keeps timestamp uniqueness for the rare
        # case where the same ms boundary is hit.
        written_new_paths: list[Path] = []
        for wb, _emb in kept_new:
            fname = f"sample_{origin}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.wav"
            fpath = voice_dir / fname
            fpath.write_bytes(wb)
            written_new_paths.append(fpath)

        # Step 7 — Aggregate the per-sample embeddings we already computed
        # in Steps 2 + 3 instead of issuing one more /embed call with every
        # kept WAV. Bundling all samples into one server call would concat
        # them into a single waveform before VAD/chunking — a sample-level
        # boundary loss that differs from what dlbackend's own register()
        # does (preprocess each file separately, then pool chunks). Each
        # per-sample embedding is already L2-normalized, so the self-
        # consistency weighted mean below matches the server's aggregation
        # math, minus the cross-file concat artifact.
        kept_embeddings = [emb for _p, emb in kept_existing] + [
            emb for _wb, emb in kept_new
        ]
        embedding = _weighted_aggregate(kept_embeddings)
        np.save(self._embedding_path(norm), embedding)

        # Re-read the folder — it now contains ONLY samples that were valid
        # AND passed the consistency filter (kept existing + written new).
        all_samples = sorted(voice_dir.glob("sample_*.wav"))

        logger.info(
            "Enroll committed: new_written=%d new_rejected=%d existing_kept=%d existing_dropped=%d total_on_disk=%d dim=%d",
            len(written_new_paths), dropped_new,
            len(kept_existing), len(dropped_existing),
            len(all_samples), int(embedding.shape[0]),
        )

        # Update voice metadata + registry.
        existing = self._read_metadata(norm)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        enrollment_sources = sorted(
            {_sample_origin(p.name) for p in all_samples} | {origin}
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
            "num_samples": len(all_samples),
            "sample_files": [p.name for p in all_samples],
            "sample_origins": {p.name: _sample_origin(p.name) for p in all_samples},
            "embedding_dim": int(embedding.shape[0]),
        }
        self._write_metadata(norm, meta)
        self._update_registry(norm, meta)

        # Drop any stranger clusters whose WAVs were just enrolled. Keeping
        # them would leave stale centroids that re-label the now-known
        # speaker as voice_<N> on any recognition below the main threshold.
        if source_type == "filepath":
            self._drop_consumed_clusters(sources)

        logger.info(
            "Enrolled speaker '%s' — %d total samples, dim=%d",
            norm,
            meta["num_samples"],
            meta["embedding_dim"],
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

        saved_path = self._save_incoming_audio(wav_bytes)

        if not self.available:
            logger.warning("Embedding server not configured — set SPEAKER_EMBEDDING_API_URL or DL_BACKEND_URL")
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
            # dlbackend's /recognize uses internally, so per-chunk voting
            # below produces apples-to-apples confidence.
            query_chunks = self._call_embedding_api(
                payload, return_chunks=True
            )  # [M, D]
        except SpeakerRecognizerError as e:
            logger.warning(
                "Recognize: embedding failed for %s — %s", saved_path, e,
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

        known = self._load_all_embeddings()
        if not known:
            # No enrolled users — every voice is unknown. Still assign a
            # stable cluster hash so repeat speakers can be tracked before
            # anyone is enrolled.
            logger.info("Recognize: no enrolled users — unknown + cluster-only path")
            vp_hash = self._assign_voiceprint_hash(query_chunks)
            saved_path = self._move_to_cluster(saved_path, vp_hash)
            logger.info(
                "Recognize result: name=unknown confidence=0.00 cluster=%s path=%s",
                vp_hash or "(none)", saved_path,
            )
            return {
                "name": "unknown",
                "confidence": 0.0,
                "match": False,
                "unknown_audio_path": saved_path,
                "voiceprint_hash": vp_hash or None,
                "candidates": [],
            }

        # Per-chunk voting (mirrors dlbackend.recognize line 614-645):
        # for each query chunk, pick the highest-confidence speaker, record
        # one vote and one confidence sample. Winner = most votes, tiebreak
        # by avg confidence. Returned confidence = avg of winner's votes.
        names = list(known.keys())
        ref_matrix = np.stack([known[n] for n in names], axis=0)  # [K, D]
        sims = query_chunks @ ref_matrix.T                         # [M, K] raw cos
        confs = (sims + 1.0) / 2.0                                  # mapped [0, 1]
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
        vp_hash = None if is_match else (self._assign_voiceprint_hash(query_chunks) or None)
        # Move WAV into per-cluster sub-dir so later inspection can group
        # samples by cluster. Known-speaker WAVs stay in the flat dir.
        if vp_hash:
            saved_path = self._move_to_cluster(saved_path, vp_hash)

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
        if not self._embedding_path(norm).is_file():
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
            "num_samples": voice_meta.get("num_samples", 0),
            "embedding_dim": voice_meta.get("embedding_dim", 0),
            "enrolled_at": voice_meta.get("enrolled_at"),
            "updated_at": voice_meta.get("updated_at"),
            "sample_files": voice_meta.get("sample_files", []),
            "sample_origins": voice_meta.get("sample_origins", {}),
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
            if not self._embedding_path(norm).is_file():
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
                    "num_samples": voice_meta.get("num_samples", 0),
                    "embedding_dim": voice_meta.get("embedding_dim", 0),
                    "enrolled_at": voice_meta.get("enrolled_at"),
                    "updated_at": voice_meta.get("updated_at"),
                    "sample_files": voice_meta.get("sample_files", []),
                    "sample_origins": voice_meta.get("sample_origins", {}),
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
        if not self._embedding_path(norm).is_file():
            raise SpeakerRecognizerError(
                f"no voice profile for '{norm}' — call enroll first"
            )
        shared = _merge_shared_metadata(
            user_dir,
            display_name=name.strip() or None,
            telegram_username=telegram_username or None,
            telegram_id=telegram_id or None,
        )
        # Refresh mirrored fields in voice metadata + registry.
        voice_meta = self._read_metadata(norm)
        voice_meta["telegram_username"] = shared.get("telegram_username", "")
        voice_meta["telegram_id"] = shared.get("telegram_id", "")
        voice_meta["has_telegram_identity"] = bool(
            shared.get("telegram_id") or shared.get("telegram_username")
        )
        voice_meta["display_name"] = shared.get(
            "display_name", voice_meta.get("display_name", norm)
        )
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
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

        We always save — even on a match — so that skills have a stable path
        to reuse for follow-up enrollment flows.
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
        return str(fpath)

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
            return str(dst)
        except OSError as e:
            logger.warning(
                "move %s to cluster %s failed: %s", saved_path, vp_hash, e,
            )
            return saved_path

    # ------------------------------------------------- voice stranger clustering

    def _assign_voiceprint_hash(self, query_chunks: np.ndarray) -> str:
        """Return a stable voice_<N> label for an unknown voice.

        Aggregates the per-chunk query embeddings into one L2-normalized
        vector, then compares against saved stranger centroids. A match
        (cosine >= _VOICE_STRANGER_MATCH_THRESHOLD) reuses the existing
        label; otherwise a new label is allocated and persisted.

        Consumers don't call this directly — recognize() stamps the hash
        into its response when the speaker is unknown.
        """
        if query_chunks is None or len(query_chunks) == 0:
            return ""
        agg = query_chunks.mean(axis=0)
        norm = float(np.linalg.norm(agg))
        if norm == 0.0:
            return ""
        agg = agg / norm

        with self._stranger_lock:
            best_sim_pre = None  # captured for the "new cluster" log path
            best_label_pre: Optional[str] = None
            breakdown_pre = ""
            if self._stranger_embeds is not None and len(self._stranger_embeds) > 0:
                # Both sides L2-normalized → dot product is raw cosine [-1, 1].
                # Convert to scaled [0, 1] so the threshold sits in the same
                # unit as MATCH / CONSISTENCY elsewhere in the file.
                raw_sims = self._stranger_embeds @ agg
                sims = (raw_sims + 1.0) / 2.0
                best_idx = int(np.argmax(sims))
                best_sim = float(sims[best_idx])
                # Capture full breakdown for diagnostics — this is the same
                # data the matching loop sees, so we can surface it whether
                # we matched or fell through to "new cluster".
                breakdown_pre = ", ".join(
                    f"{self._stranger_labels[i]}={float(sims[i]):.3f}"
                    for i in range(len(sims))
                )
                best_sim_pre = best_sim
                best_label_pre = str(self._stranger_labels[best_idx])
                if best_sim >= _VOICE_STRANGER_MATCH_THRESHOLD:
                    logger.info(
                        "Voiceprint hash: %s (matched existing cluster, "
                        "sim=%.3f, threshold=%.3f) | scores=[%s]",
                        best_label_pre, best_sim,
                        _VOICE_STRANGER_MATCH_THRESHOLD, breakdown_pre,
                    )
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

            # Evict oldest entries once over the cap. Keeps disk bounded
            # without impacting recent speakers the agent still cares about.
            if len(self._stranger_embeds) > _MAX_VOICE_STRANGERS:
                drop = len(self._stranger_embeds) - _MAX_VOICE_STRANGERS
                self._stranger_embeds = self._stranger_embeds[drop:]
                self._stranger_labels = self._stranger_labels[drop:]

            self._save_strangers()
            if best_sim_pre is not None:
                # Hit the "no existing cluster matched" branch — surface the
                # closest miss so operators can spot threshold edge cases
                # (e.g. same speaker scoring 0.66 vs threshold 0.675).
                logger.info(
                    "Voiceprint hash: %s (new cluster, total=%d) | "
                    "closest=%s sim=%.3f below threshold=%.3f | scores=[%s]",
                    label, len(self._stranger_embeds),
                    best_label_pre, best_sim_pre,
                    _VOICE_STRANGER_MATCH_THRESHOLD, breakdown_pre,
                )
            else:
                logger.info(
                    "Voiceprint hash: %s (new cluster, total=%d) | "
                    "no prior clusters",
                label, len(self._stranger_embeds),
            )
            return label

    def _match_stranger_clusters(
        self, query_embedding: np.ndarray, threshold: float,
    ) -> list[str]:
        """Return stranger labels whose centroid cosine-sim to query > threshold.

        Used by enroll() to auto-include clusters that belong to the same
        speaker but fragmented (e.g. distance / volume drift pushed centroids
        below STRANGER_MATCH so they landed in different clusters even though
        it's one person). Pass a looser threshold than STRANGER_MATCH so the
        fragmented siblings get pulled back together; the consistency filter
        downstream in enroll() handles any false positive.

        ``threshold`` is SCALED cosine in [0, 1] — same unit as MATCH /
        CONSISTENCY / STRANGER_MATCH.
        """
        q = _l2(query_embedding)
        if float(np.linalg.norm(q)) == 0.0:
            logger.info("Auto-merge scan: query embedding has zero norm — skip")
            return []
        with self._stranger_lock:
            if (
                self._stranger_embeds is None
                or self._stranger_labels is None
                or len(self._stranger_embeds) == 0
            ):
                logger.info("Auto-merge scan: no stranger centroids to compare")
                return []
            # Both sides L2-normalized → dot product is raw cosine [-1, 1].
            # Convert to scaled [0, 1] so the threshold lives in one unit
            # across the file.
            sims = (self._stranger_embeds @ q + 1.0) / 2.0
            # Log every cluster's scaled similarity (not just matches) so
            # operators can tune threshold from real data: e.g. see voice_5
            # missed at sim=0.62 and decide to lower threshold to 0.60.
            breakdown = ", ".join(
                f"{self._stranger_labels[i]}={float(sims[i]):.3f}"
                for i in range(len(sims))
            )
            logger.info(
                "Auto-merge scan: threshold=%.2f query=[%s]",
                threshold, breakdown,
            )
            return [
                str(self._stranger_labels[i])
                for i, s in enumerate(sims)
                if float(s) > threshold
            ]

    def _save_strangers(self) -> None:
        """Persist stranger state to disk. Caller must hold _stranger_lock."""
        if self._stranger_embeds is None or self._stranger_labels is None:
            return
        try:
            np.save(_VOICE_STRANGERS_DIR / "embeds.npy", self._stranger_embeds)
            np.save(_VOICE_STRANGERS_DIR / "labels.npy", self._stranger_labels)
            np.save(
                _VOICE_STRANGERS_DIR / "counter.npy",
                np.array(self._stranger_counter),
            )
        except OSError as e:
            logger.warning("save voice strangers failed: %s", e)

    def _load_strangers(self) -> None:
        """Load stranger state from disk on startup. Silent on missing files."""
        embeds_path = _VOICE_STRANGERS_DIR / "embeds.npy"
        labels_path = _VOICE_STRANGERS_DIR / "labels.npy"
        counter_path = _VOICE_STRANGERS_DIR / "counter.npy"
        if not (embeds_path.exists() and labels_path.exists()):
            return
        try:
            self._stranger_embeds = np.load(embeds_path)
            self._stranger_labels = np.load(labels_path)
            if counter_path.exists():
                self._stranger_counter = int(np.load(counter_path))
            logger.info(
                "Loaded %d voice strangers (counter=%d)",
                len(self._stranger_embeds), self._stranger_counter,
            )
        except Exception as e:
            logger.warning("load voice strangers failed: %s", e)
            self._stranger_embeds = None
            self._stranger_labels = None
            self._stranger_counter = 0
