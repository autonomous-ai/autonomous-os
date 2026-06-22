from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
import torch

from core.export.utils.constants import AUDIO_DIR, IMAGES_DIR


def prepare_onnx_session(onnx_model: Path):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 0
    opts.inter_op_num_threads = 0
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.add_session_config_entry("session.dynamic_block_base", "4")

    session = ort.InferenceSession(
        onnx_model,
        sess_options=opts,
        providers=["CPUExecutionProvider"],
    )
    return session


def shrink_to_same_shape(*arrays: npt.NDArray[np.number]):
    min_shape = np.array([a.shape for a in arrays]).min(axis=0)
    return tuple([a[tuple(slice(0, s) for s in min_shape)] for a in arrays])


def evaluate_image(
    original_model: torch.nn.Module,
    onnx_model: Path,
    input_size: tuple[int, int],
    original_kwargs: dict[str, Any] | None = None,
    onnx_kwargs: dict[str, Any] | None = None,
):
    original_kwargs = original_kwargs or {}
    onnx_kwargs = onnx_kwargs or {}

    images: list[cv2.typing.MatLike] = [np.random.rand(*input_size, 3).astype(np.float32)]
    if IMAGES_DIR.is_dir():
        for image_path in IMAGES_DIR.glob("*"):
            if not image_path.is_file():
                continue

            image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)  # H, W, C
            if image is None:
                continue

            image = cv2.resize(image, input_size)
            image = image.astype(np.float32) / 255.0
            images.append(image)

    images_batch = np.stack(images, axis=0)  # B, H, W, C
    images_batch = images_batch.transpose(0, 3, 1, 2)  # B, C, H, W

    images_tensor = torch.Tensor(images_batch)
    with torch.no_grad():
        original_result = original_model(images_tensor, **original_kwargs)

    onnx_session = prepare_onnx_session(onnx_model)
    input_name = onnx_session.get_inputs()[0].name
    onnx_result = onnx_session.run(None, {input_name: images_batch, **onnx_kwargs})

    # PyTorch may return a single tensor or a tuple; ONNX always returns a list
    if not isinstance(original_result, (tuple, list)):
        original_result = (original_result,)

    errors: list[tuple[float, float]] = []
    for orig, onnx in zip(original_result, onnx_result):
        orig_np = cast(npt.NDArray[np.float32], orig.cpu().numpy())
        onnx_np = cast(npt.NDArray[np.float32], onnx)
        orig_np, onnx_np = shrink_to_same_shape(orig_np, onnx_np)

        mean_error = np.mean(np.abs(orig_np - onnx_np)).item()
        max_error = np.max(np.abs(orig_np - onnx_np)).item()
        errors.append((mean_error, max_error))

    return errors


def _load_audio(path: Path, target_sr: int = 16000) -> npt.NDArray[np.float32]:
    """Load audio file as mono float32 numpy array at target_sr."""
    import soundfile as sf

    waveform, sr = sf.read(path, dtype="float32")
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    if sr != target_sr:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(sr, target_sr)
        waveform = resample_poly(waveform, target_sr // g, sr // g).astype(np.float32)
    return waveform


def evaluate_audio(
    original_model: torch.nn.Module,
    onnx_model: Path,
    sample_rate: int = 16000,
):
    waveforms: list[npt.NDArray[np.float32]] = []
    if AUDIO_DIR.is_dir():
        for audio_path in sorted(AUDIO_DIR.rglob("*.wav")):
            waveforms.append(_load_audio(audio_path, target_sr=sample_rate))

    if not waveforms:
        # Generate a dummy waveform if no audio files found
        waveforms.append(np.random.randn(sample_rate * 2).astype(np.float32))

    onnx_session = prepare_onnx_session(onnx_model)

    errors: list[tuple[float, float]] = []
    for waveform in waveforms:
        audio = waveform[np.newaxis]  # (1, T)

        with torch.no_grad():
            original_result = original_model(torch.Tensor(audio))

        input_name = onnx_session.get_inputs()[0].name
        onnx_result = onnx_session.run(None, {input_name: audio})

        if not isinstance(original_result, (tuple, list)):
            original_result = (original_result,)

        for orig, onnx in zip(original_result, onnx_result):
            orig_np = cast(npt.NDArray[np.float32], orig.cpu().numpy())
            onnx_np = cast(npt.NDArray[np.float32], onnx)

            mean_error = np.mean(np.abs(orig_np - onnx_np)).item()
            max_error = np.max(np.abs(orig_np - onnx_np)).item()
            errors.append((mean_error, max_error))

    return errors


def evaluate_video(
    original_model: torch.nn.Module,
    onnx_model: Path,
    input_shape: tuple[int, ...],
    n_samples: int = 4,
):
    """Evaluate video model (input range [0,1])."""
    dummy = np.random.rand(n_samples, *input_shape).astype(np.float32)

    with torch.no_grad():
        original_result = original_model(torch.Tensor(dummy))

    onnx_session = prepare_onnx_session(onnx_model)
    input_name = onnx_session.get_inputs()[0].name
    onnx_result = onnx_session.run(None, {input_name: dummy})

    if not isinstance(original_result, (tuple, list)):
        original_result = (original_result,)

    errors: list[tuple[float, float]] = []
    for orig, onnx in zip(original_result, onnx_result):
        orig_np = cast(npt.NDArray[np.float32], orig.cpu().numpy())
        onnx_np = cast(npt.NDArray[np.float32], onnx)

        mean_error = np.mean(np.abs(orig_np - onnx_np)).item()
        max_error = np.max(np.abs(orig_np - onnx_np)).item()
        errors.append((mean_error, max_error))

    return errors


def evaluate_skeleton(
    original_model: torch.nn.Module,
    onnx_model: Path,
    input_shape: tuple[int, ...],
    n_samples: int = 4,
):
    """Evaluate skeleton model (input is raw coordinates, can be negative)."""
    dummy = np.random.randn(n_samples, *input_shape).astype(np.float32)

    with torch.no_grad():
        original_result = original_model(torch.Tensor(dummy))

    onnx_session = prepare_onnx_session(onnx_model)
    input_name = onnx_session.get_inputs()[0].name
    onnx_result = onnx_session.run(None, {input_name: dummy})

    if not isinstance(original_result, (tuple, list)):
        original_result = (original_result,)

    errors: list[tuple[float, float]] = []
    for orig, onnx in zip(original_result, onnx_result):
        orig_np = cast(npt.NDArray[np.float32], orig.cpu().numpy())
        onnx_np = cast(npt.NDArray[np.float32], onnx)

        mean_error = np.mean(np.abs(orig_np - onnx_np)).item()
        max_error = np.max(np.abs(orig_np - onnx_np)).item()
        errors.append((mean_error, max_error))

    return errors
