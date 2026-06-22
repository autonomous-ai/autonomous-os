"""Model file utilities — ensure models are downloaded before use."""

import logging
import os
import shutil
import urllib.request
from pathlib import Path

from core.enums.files import ModelEnum

logger: logging.Logger = logging.getLogger(__name__)

# Manifest mapping each supported model to its object path within the public
# weights bucket. The full download URL is `settings.cdn_base` + this path
# (see `get_default_cdn_url`); the local cache filename is just the basename.
# Weights are NOT committed to the repo — they are fetched on first use.
# Keep this in sync with dlbackend/docs/configuration.md ("Model downloading").
CDN_PATHS: dict[ModelEnum, str] = {
    # Action recognition
    ModelEnum.X3D: "onnx_models/x3d_m_16x5x1_int8.onnx",
    ModelEnum.VIDEOMAE: "onnx_models/videomae_fp32.onnx",
    ModelEnum.UNIFORMERV2: "onnx_models/uniformerv2-l-224-k400_fp32.onnx",
    # Facial emotion (FER)
    ModelEnum.POSTERV2: "onnx_models/posterv2_7cls.onnx",
    ModelEnum.EMONET_8: "onnx_models/emonet_8.onnx",
    ModelEnum.EMONET_5: "onnx_models/emonet_5.onnx",
    # Audio emotion (SER)
    ModelEnum.EMOTION2VEC: "onnx_models/emotion2vec.onnx",
    # Pose 2D
    ModelEnum.RTMPOSE_M: "onnx_models/rtmpose-m.onnx",
    # Pose 3D
    ModelEnum.TCPFORMER_H36M_243: "onnx_models/tcpformer_h36m_243.onnx",
    # Audio embedder (WeSpeaker)
    ModelEnum.WESPEAKER_RESNET34: "onnx_models/wespeaker_resnet34.onnx",
    ModelEnum.WESPEAKER_ECAPA_TDNN_1024: "onnx_models/wespeaker_ecapa_tdnn1024.onnx",
    ModelEnum.WESPEAKER_CAMPPLUS: "onnx_models/wespeaker_campplus.onnx",
    # Face detection
    ModelEnum.YUNET: "onnx_models/face_detection_yunet_2023mar.onnx",
    # Person detection
    ModelEnum.YOLO_PERSON: "onnx_models/yolo12x_raw.onnx",
    # Object detection
    ModelEnum.YOLO_WORLD: "onnx_models/yolov8x-worldv2_raw.onnx",
    ModelEnum.OWLV2: "onnx_models/owlv2_raw.onnx",
    ModelEnum.GROUNDING_DINO: "onnx_models/grounding_dino_raw.onnx",
}


def get_models_cache_dir() -> Path:
    """Get models cache dir from settings."""
    from config import settings

    return settings.model_cache_dir


def get_default_cdn_url(model: ModelEnum) -> str | None:
    """Build the full CDN URL for a given model from settings.cdn_base + CDN_PATHS."""
    path: str | None = CDN_PATHS.get(model)
    if path is None:
        return None
    from config import settings

    return f"{settings.cdn_base.rstrip('/')}/{path}"


def get_default_model_path(model: ModelEnum) -> Path | None:
    """Get the default local cache path for a model."""
    path: str | None = CDN_PATHS.get(model)
    if path is None:
        return None
    filename: str = Path(path).name
    return get_models_cache_dir() / filename


def ensure_downloaded(local_path: Path, remote: str | None = None) -> Path:
    """Ensure a model file exists at ``local_path``, downloading if needed.

    Args:
        local_path: Expected local file path.
        remote: URL (``http://`` / ``https://``) for direct download,
            or a HuggingFace repo ID (e.g. ``Wespeaker/wespeaker-voxceleb-resnet34-LM``)
            for ``huggingface_hub.hf_hub_download``.
            If None and file missing, raises FileNotFoundError.

    Returns:
        ``local_path`` (guaranteed to exist).

    Raises:
        FileNotFoundError: If file missing and no remote provided.
        RuntimeError: If download fails.
    """
    if local_path.exists():
        return local_path

    if remote is None:
        raise FileNotFoundError(
            f"Model file not found: {local_path}. "
            "Provide a remote URL or HuggingFace repo ID to auto-download."
        )

    if remote.startswith("http://") or remote.startswith("https://"):
        _download_url(remote, local_path)
    else:
        _download_hf(remote, local_path)

    return local_path


def _download_url(url: str, dest: Path) -> None:
    """Atomic download from a direct URL."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Download to a per-PID temp file then atomically rename into place, so a
    # crash/kill mid-download never leaves a truncated file that a later run would
    # mistake for a complete cached model. The PID suffix keeps concurrent workers
    # from clobbering each other's partial files.
    tmp: Path = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    logger.info("Downloading %s → %s", url, dest)
    try:
        with urllib.request.urlopen(url) as response, open(tmp, "wb") as out_file:
            shutil.copyfileobj(response, out_file)
        tmp.replace(dest)
        logger.info("Download complete: %s", dest)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc


def _download_hf(repo_id: str, dest: Path) -> None:
    """Download from HuggingFace Hub and symlink/copy to dest."""
    from huggingface_hub import hf_hub_download

    filename: str = dest.name
    logger.info("Downloading from HF repo %s/%s → %s", repo_id, filename, dest)
    cached_path: Path = Path(hf_hub_download(repo_id=repo_id, filename=filename))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(cached_path)
    except OSError:
        shutil.copy2(cached_path, dest)
    logger.info("Download complete: %s", dest)
