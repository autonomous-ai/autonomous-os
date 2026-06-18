from collections.abc import Sequence
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
import torch

from core.constants import AUDIO_DIR, IMAGES_DIR


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


def evaluate_image(
    original_model: torch.nn.Module, onnx_model: Path, input_size: tuple[int, int]
):
    images: list[cv2.typing.MatLike] = []
    for image_path in IMAGES_DIR.glob("*"):
        if not image_path.is_file():
            continue

        image = cv2.imread(image_path, cv2.IMREAD_COLOR)  # H, W, C
        if image is None:
            continue

        image = cv2.resize(image, input_size)
        image = image.astype(np.float32) / 255.0
        images.append(image)

    images_batch = np.stack(images, axis=0)  # B, H, W, C
    images_batch = images_batch.transpose(0, 3, 1, 2)  # B, C, H, W

    images_tensor = torch.Tensor(images_batch)
    with torch.no_grad():
        original_result = original_model(images_tensor)

    onnx_session = prepare_onnx_session(onnx_model)
    input_name = onnx_session.get_inputs()[0].name
    onnx_result = onnx_session.run(None, {input_name: images_batch})

    if not isinstance(original_result, Sequence):
        original_result = [original_result]
        onnx_result = [onnx_result]

    errors: list[tuple[float, float]] = []
    for orig, onnx in zip(original_result, onnx_result):
        orig = cast(npt.NDArray[np.float32], orig.cpu().numpy())
        onnx = cast(npt.NDArray[np.float32], onnx)

        mean_error = np.mean(np.abs(orig - onnx)).item()
        max_error = np.max(np.abs(orig - onnx)).item()
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
    for audio_path in sorted(AUDIO_DIR.rglob("*.wav")):
        waveforms.append(_load_audio(audio_path, target_sr=sample_rate))

    onnx_session = prepare_onnx_session(onnx_model)

    errors: list[tuple[float, float]] = []
    for waveform in waveforms:
        audio = waveform[np.newaxis]  # (1, T)

        with torch.no_grad():
            original_result = original_model(torch.Tensor(audio))

        input_name = onnx_session.get_inputs()[0].name
        onnx_result = onnx_session.run(None, {input_name: audio})

        if not isinstance(original_result, Sequence):
            original_result = [original_result]
            onnx_result = [onnx_result]

        for orig, onnx in zip(original_result, onnx_result):
            orig = cast(npt.NDArray[np.float32], orig.cpu().numpy())
            onnx = cast(npt.NDArray[np.float32], onnx)

            mean_error = np.mean(np.abs(orig - onnx)).item()
            max_error = np.max(np.abs(orig - onnx)).item()
            errors.append((mean_error, max_error))

    return errors

def evaluate_skeleton(
    original_model: torch.nn.Module,
    onnx_model: Path,
    input_shape: tuple[int, ...],
    n_samples: int = 4,
):
    dummy = np.random.randn(n_samples, *input_shape).astype(np.float32)

    with torch.no_grad():
        original_result = original_model(torch.Tensor(dummy))

    onnx_session = prepare_onnx_session(onnx_model)
    input_name = onnx_session.get_inputs()[0].name
    onnx_result = onnx_session.run(None, {input_name: dummy})

    if not isinstance(original_result, Sequence):
        original_result = [original_result]
        onnx_result = [onnx_result]

    errors: list[tuple[float, float]] = []
    for orig, onnx in zip(original_result, onnx_result):
        orig = cast(npt.NDArray[np.float32], orig.cpu().numpy())
        onnx = cast(npt.NDArray[np.float32], onnx)

        mean_error = np.mean(np.abs(orig - onnx)).item()
        max_error = np.max(np.abs(orig - onnx)).item()
        errors.append((mean_error, max_error))

    return errors
