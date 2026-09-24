"""TensorRT provider options: explicit profile only when requested."""

from core.utils.runtime import TrtProfile, build_providers

ALL = ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]


def _trt_opts(providers):
    return next(
        p[1] for p in providers if isinstance(p, tuple) and p[0] == "TensorrtExecutionProvider"
    )


def test_no_profile_leaves_trt_options_unchanged():
    opts = _trt_opts(build_providers(ALL))
    assert "trt_profile_min_shapes" not in opts
    assert opts["trt_engine_cache_enable"] is True


def test_profile_sets_all_three_shape_options():
    prof = TrtProfile("audio:1x32000", "audio:1x128000", "audio:1x128000")
    opts = _trt_opts(build_providers(ALL, prof))
    assert opts["trt_profile_min_shapes"] == "audio:1x32000"
    assert opts["trt_profile_opt_shapes"] == "audio:1x128000"
    assert opts["trt_profile_max_shapes"] == "audio:1x128000"


def test_cpu_only_host_ignores_profile():
    providers = build_providers(["CPUExecutionProvider"], TrtProfile("a:1", "a:1", "a:1"))
    assert providers == ["CPUExecutionProvider"]
