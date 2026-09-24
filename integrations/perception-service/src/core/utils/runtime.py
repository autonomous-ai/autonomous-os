import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

from config import settings
from core.perception.base.predictor import gpu_lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrtProfile:
    """Explicit TensorRT optimization profile, as ORT shape strings.

    Format per ORT: ``"<input>:<d0>x<d1>..."``, e.g. ``"audio:1x32000"``.
    With a profile set, ORT builds the engine once at session creation and
    never rebuilds it for an unseen input shape inside a request (#492).
    """

    min_shapes: str
    opt_shapes: str
    max_shapes: str


def build_providers(
    available: list[str], trt_profile: TrtProfile | None = None
) -> list[str | tuple[str, dict]]:
    """TensorRT > CUDA > CPU provider list for the given available providers."""
    providers: list[str | tuple[str, dict]] = []

    if "TensorrtExecutionProvider" in available:
        trt_cache: str = str(settings.cache_dir / "trt_engines")
        Path(trt_cache).mkdir(parents=True, exist_ok=True)
        trt_opts: dict = {
            "device_id": 0,
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": trt_cache,
            "trt_timing_cache_enable": True,
            "trt_timing_cache_path": trt_cache,
            "trt_builder_optimization_level": 3,
            "trt_max_workspace_size": 2 << 30,
            "trt_layer_norm_fp32_fallback": True,
            "trt_cuda_graph_enable": False,
        }
        if trt_profile is not None:
            trt_opts["trt_profile_min_shapes"] = trt_profile.min_shapes
            trt_opts["trt_profile_opt_shapes"] = trt_profile.opt_shapes
            trt_opts["trt_profile_max_shapes"] = trt_profile.max_shapes
        providers.append(("TensorrtExecutionProvider", trt_opts))

    if "CUDAExecutionProvider" in available:
        providers.append(
            (
                "CUDAExecutionProvider",
                {
                    "arena_extend_strategy": "kSameAsRequested",
                    "cudnn_conv_algo_search": "DEFAULT",
                    "do_copy_in_default_stream": True,
                    "cudnn_conv_use_max_workspace": False,
                },
            )
        )

    providers.append("CPUExecutionProvider")
    return providers


def prepare_ort_session(
    model_path: Path,
    *,
    warmup_inputs: dict[str, np.ndarray] | Sequence[dict[str, np.ndarray]] | None = None,
    trt_profile: TrtProfile | None = None,
) -> ort.InferenceSession:
    """Create an ONNX Runtime session with TensorRT > CUDA > CPU fallback.

    Args:
        model_path: Path to the ONNX model file.
        warmup_inputs: One input dict, or several (each run n_warmup times).
        trt_profile: Explicit TensorRT shape profile; engine is built at
            session creation and never rebuilt per request.
    """
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 0
    opts.inter_op_num_threads = 0
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.add_session_config_entry("session.dynamic_block_base", "4")
    # Force single-threaded execution to prevent CUDA stream collisions
    # under concurrent requests (the global _gpu_lock serializes at Python level,
    # but ORT's internal thread pool can still launch parallel CUDA kernels)
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    providers = build_providers(ort.get_available_providers(), trt_profile)

    with gpu_lock:
        session = ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
        logger.info(
            "ONNX session created for %s — providers: %s",
            model_path.name,
            [p if isinstance(p, str) else p[0] for p in session.get_providers()],
        )

        if warmup_inputs is not None:
            shapes = [warmup_inputs] if isinstance(warmup_inputs, dict) else list(warmup_inputs)
            n_warmup = 3
            logger.info(
                "Warming up ONNX session for %s (%d shape(s) x %d runs)",
                model_path.name,
                len(shapes),
                n_warmup,
            )
            for inputs in shapes:
                for _ in range(n_warmup):
                    session.run(None, inputs)
            logger.info("Warmup complete for %s", model_path.name)

    return session
