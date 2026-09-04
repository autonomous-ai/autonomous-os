"""Face-model paths + download-on-first-use from cloud storage.

The ONNX weights for the v2 face pipeline (SCRFD detector, EdgeFace embedder,
MediaPipe landmark regressor) are NOT committed to the repo. They are fetched on
first use into a local cache directory (default ``/root/local/models``, the same
convention as ``rtmpose-m.onnx`` / ``HAL_POSE_MODEL_PATH`` in ``config.py``).

Remote layout mirrors the perception-service weights bucket
(``integrations/perception-service/src/core/utils/files.py``): each model lives
at ``<cdn_base>/onnx_models/<filename>`` in the public Google Cloud Storage
bucket. The local cache filename is just the basename.

    default cdn_base : https://storage.googleapis.com/autonomous-models
    scrfd            : onnx_models/scrfd_2.5g_fp32.onnx
    edgeface         : onnx_models/edgeface_s_gamma_05_opt.onnx
    landmark         : onnx_models/MediaPipeFaceLandmarkDetector.onnx

Everything is overridable by env var so a device can point at a mirror or a
pre-provisioned path:

    HAL_FACE_MODEL_PATH            base cache dir (default /root/local/models)
    HAL_FACE_SCRFD_MODEL_PATH      full path to the SCRFD model
    HAL_FACE_EDGEFACE_MODEL_PATH   full path to the EdgeFace model
    HAL_FACE_LANDMARK_MODEL_PATH   full path to the landmark model
    HAL_FACE_MODEL_CDN_BASE        weights bucket base URL
    HAL_FACE_LANDMARK_CONF_THRESHOLD  face-presence gate for alignment

NOTE: the face weights are not uploaded to the bucket yet. Until they are,
``ensure_face_models`` will raise a clear RuntimeError on a device that has no
local copy — the download path is in place and takes over automatically once the
objects exist at the URLs above.
"""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Base cache dir. Mirrors POSE_MOTION_MODEL_PATH's /root/local/models convention.
# HAL_FACE_MODEL_PATH is kept as the (dir) override name for back-compat.
_FACE_MODEL_DIR: str = os.environ.get("HAL_FACE_MODEL_PATH", "/root/local/models")

_SCRFD_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_SCRFD_MODEL_PATH", os.path.join(_FACE_MODEL_DIR, "scrfd_2.5g_fp32.onnx")
)
_EDGEFACE_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_EDGEFACE_MODEL_PATH",
    os.path.join(_FACE_MODEL_DIR, "edgeface_s_gamma_05_opt.onnx"),
)
# MediaPipe FaceMesh landmark regressor exported to ONNX (replaces the pip
# `mediapipe` dependency, which cannot be installed on the target device).
_LANDMARK_MODEL_PATH: str = os.environ.get(
    "HAL_FACE_LANDMARK_MODEL_PATH",
    os.path.join(_FACE_MODEL_DIR, "MediaPipeFaceLandmarkDetector.onnx"),
)

# Face-presence probability above which the ONNX landmarks are trusted for
# alignment; below it the detection is dropped (no SCRFD keypoint fallback), so
# this doubles as a false-alarm gate. See _OnnxLandmarkAligner.
#
# 0.99, not 0.6: the model's score SATURATES. Measured over 990 logged frames on
# lamp-ac82 (04/09/2026) the median is exactly 1.000 and the minimum is 0.613 —
# so at 0.6 this gate had never once fired, and the "non-face detections are
# gated out" property it exists for was not actually in effect.
#
# What it catches at 0.99 is a detection that is facial but carries NO identity:
# SCRFD firing on an ear at close range. 26 such frames appeared in one 40-minute
# session, all at ~0.0 similarity to the user's own enrollment photo; one minted
# a stranger identity and the other 25 then matched it. landmark_score separates
# them from real faces at AUC 0.98, and 0.99 removes all 26 — taking that
# session's spurious identities from 1 to 0. It drops 70 of 990 frames, and
# recognition RISES 94.6% -> 97.8% because those frames leave the denominator.
#
# Tuned on one device: lower it via the env var if a deployment starts logging
# FAIL-* folders for faces that are plainly fine.
_LANDMARK_CONF_THRESHOLD: float = float(
    os.environ.get("HAL_FACE_LANDMARK_CONF_THRESHOLD", "0.99")
)

# Public weights bucket base URL (matches perception-service settings.cdn_base).
_CDN_BASE: str = os.environ.get(
    "HAL_FACE_MODEL_CDN_BASE", "https://storage.googleapis.com/autonomous-models"
)

# Model filename -> object path within the weights bucket. The full download URL
# is ``<cdn_base>/<object path>``. Keyed by basename so an env-overridden local
# path still resolves to the right remote as long as the filename is unchanged.
_CDN_OBJECTS: dict[str, str] = {
    "scrfd_2.5g_fp32.onnx": "onnx_models/scrfd_2.5g_fp32.onnx",
    "edgeface_s_gamma_05_opt.onnx": "onnx_models/edgeface_s_gamma_05_opt.onnx",
    "MediaPipeFaceLandmarkDetector.onnx": "onnx_models/MediaPipeFaceLandmarkDetector.onnx",
}


def _remote_for(local_path: Path) -> str | None:
    """Full CDN URL for a model, resolved by its basename (or None if unknown)."""
    obj = _CDN_OBJECTS.get(local_path.name)
    if obj is None:
        return None
    return f"{_CDN_BASE.rstrip('/')}/{obj}"


def _download_url(url: str, dest: Path) -> None:
    """Atomic download from a direct URL.

    Downloads to a per-PID temp file then atomically renames into place, so a
    crash/kill mid-download never leaves a truncated file that a later run would
    mistake for a complete cached model. The PID suffix keeps concurrent workers
    from clobbering each other's partial files.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("[face-v2] downloading %s -> %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("[face-v2] download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def ensure_downloaded(local_path: Path, remote: str | None) -> Path:
    """Ensure a model file exists at ``local_path``, downloading if needed.

    Returns ``local_path`` (guaranteed to exist on success).

    Raises:
        FileNotFoundError: file missing and no remote URL could be resolved.
        RuntimeError: download attempted but failed.
    """
    if local_path.exists():
        return local_path
    if remote is None:
        raise FileNotFoundError(
            f"Face model not found: {local_path}. No download URL is known for "
            f"'{local_path.name}' — set the matching HAL_FACE_*_MODEL_PATH env var "
            "to a pre-provisioned file."
        )
    _download_url(remote, local_path)
    return local_path


def ensure_face_models(*model_paths: str) -> None:
    """Ensure each given model path exists locally, fetching from the weights
    bucket on first use.

    Called once at pipeline startup (``FaceRecognizer.start``). Missing weights
    are downloaded into the local cache dir; a model with no known remote (custom
    filename) and no local copy raises FileNotFoundError.
    """
    for raw in model_paths:
        path = Path(raw)
        if path.exists():
            continue
        ensure_downloaded(path, _remote_for(path))
