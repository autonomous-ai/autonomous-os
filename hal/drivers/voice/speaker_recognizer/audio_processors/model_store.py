"""STOI model path + download-on-first-use from cloud storage.

The SQUIM-STOI ONNX weight is NOT committed to the repo (it's ~20 MB). It is
fetched on first use into the local cache dir (default ``/root/local/models``,
the same convention as ``rtmpose-m.onnx`` / the faceid weights — see
``hal/drivers/sensing/perceptions/processors/faceid/model_store.py``).

Remote layout mirrors the perception-service weights bucket: the model lives at
``<cdn_base>/onnx_models/<filename>`` in the public Google Cloud Storage bucket.

    default cdn_base : https://storage.googleapis.com/autonomous-models
    stoi             : onnx_models/squimm_stoi.onnx

Overridable by env var:

    HAL_SPEAKER_MODEL_CDN_BASE   weights bucket base URL
    HAL_SPEAKER_PROC_STOI_MODEL_PATH   full local path (see hal/config.py)
"""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Public weights bucket base URL (matches perception-service settings.cdn_base
# and the faceid model store).
_CDN_BASE: str = os.environ.get(
    "HAL_SPEAKER_MODEL_CDN_BASE", "https://storage.googleapis.com/autonomous-models"
)

# Model filename -> object path within the weights bucket. Full download URL is
# ``<cdn_base>/<object path>``. Keyed by basename so an env-overridden local
# path still resolves to the right remote as long as the filename is unchanged.
# NOTE: confirm this object path matches what was uploaded to the bucket.
_CDN_OBJECTS: dict[str, str] = {
    "squimm_stoi.onnx": "onnx_models/squimm_stoi.onnx",
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
    crash/kill mid-download never leaves a truncated file a later run would
    mistake for a complete cached model.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("[stoi] downloading %s -> %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("[stoi] download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def ensure_stoi_model(model_path: str) -> str:
    """Ensure the STOI model exists at ``model_path``, downloading if needed.

    Returns the path (guaranteed to exist on success). Raises FileNotFoundError
    (no remote known for the basename) or RuntimeError (download failed) — the
    caller (AudioProcessorFactory) catches these and simply skips the STOI gate,
    so an unreachable CDN degrades to "no quality gate" rather than breaking
    recognition.
    """
    path = Path(model_path)
    if path.exists():
        return str(path)
    remote = _remote_for(path)
    if remote is None:
        raise FileNotFoundError(
            f"STOI model not found: {path}. No download URL is known for "
            f"'{path.name}' — set HAL_SPEAKER_PROC_STOI_MODEL_PATH to a "
            "pre-provisioned file, or add the basename to _CDN_OBJECTS."
        )
    _download_url(remote, path)
    return str(path)
