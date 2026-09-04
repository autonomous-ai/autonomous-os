# On-Device Model Weights (HAL)

HAL runs several ONNX models on the device (face recognition, 2D pose, …). Their
weight files are large binaries and are **not committed to git**. This page
documents where they live, how the face-recognition weights are fetched on first
use, and the environment variables that control both.

> Code: `hal/drivers/sensing/perceptions/processors/faceid/model_store.py`
> (paths + download) and `.../faceid/recognizer.py` (`FaceRecognizer.start`,
> the first-use trigger).

## Cache location

All on-device weights live in a single cache directory, default
**`/root/local/models/`**. This mirrors the pose-model convention
(`HAL_POSE_MODEL_PATH` defaults to `/root/local/models/rtmpose-m.onnx`, see
`hal/config.py`). The cache filename is always the model's basename.

## Face-recognition models

The v2 face pipeline (`faceid/`) uses three ONNX models:

| Role | File | Remote object (in bucket) | Path override env var |
|------|------|---------------------------|-----------------------|
| SCRFD face detector | `scrfd_2.5g_fp32.onnx` | `onnx_models/scrfd_2.5g_fp32.onnx` | `HAL_FACE_SCRFD_MODEL_PATH` |
| EdgeFace embedder | `edgeface_s_gamma_05_opt.onnx` | `onnx_models/edgeface_s_gamma_05_opt.onnx` | `HAL_FACE_EDGEFACE_MODEL_PATH` |
| MediaPipe landmark regressor | `MediaPipeFaceLandmarkDetector.onnx` | `onnx_models/MediaPipeFaceLandmarkDetector.onnx` | `HAL_FACE_LANDMARK_MODEL_PATH` |

The full download URL for each is `<cdn_base>/<remote object>`, i.e. by default
`https://storage.googleapis.com/autonomous-models/onnx_models/<file>`. The remote
layout matches the cloud perception-service weights bucket
(`integrations/perception-service/src/core/utils/files.py`).

## Download on first use

The weights are fetched lazily — nothing downloads at import time.

1. When `FaceRecognizer.start()` runs (once, before the ONNX sessions are built),
   it calls `ensure_face_models(...)` with the three model paths.
2. For each path that does **not** already exist locally, the remote URL is
   resolved from the file's basename and the file is downloaded into the cache
   dir.
3. The download is **atomic**: it writes to a per-PID temp file
   (`<name>.part.<pid>`) and renames it into place on success, so a crash or kill
   mid-download never leaves a truncated file that a later run mistakes for a
   complete model.

Failure modes:

- **Unknown filename with no local copy** → `FileNotFoundError` (no remote can be
  resolved; point the matching `HAL_FACE_*_MODEL_PATH` at a pre-provisioned file).
- **Download error** (network/404) → `RuntimeError`.

> **Status:** the face weights are **not uploaded to the bucket yet**. Until they
> are, a device with no local copy raises on `start()`. The download path is
> already in place and takes over automatically once the objects exist at the
> URLs above — no code change needed.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HAL_FACE_MODEL_PATH` | `/root/local/models` | Base cache dir for face weights |
| `HAL_FACE_SCRFD_MODEL_PATH` | `<dir>/scrfd_2.5g_fp32.onnx` | Full path to the SCRFD model |
| `HAL_FACE_EDGEFACE_MODEL_PATH` | `<dir>/edgeface_s_gamma_05_opt.onnx` | Full path to the EdgeFace model |
| `HAL_FACE_LANDMARK_MODEL_PATH` | `<dir>/MediaPipeFaceLandmarkDetector.onnx` | Full path to the landmark model |
| `HAL_FACE_MODEL_CDN_BASE` | `https://storage.googleapis.com/autonomous-models` | Weights bucket base URL |
| `HAL_FACE_LANDMARK_CONF_THRESHOLD` | `0.99` | Face-presence gate for landmark alignment. The score saturates (median exactly 1.000 over 990 logged frames), so the useful range is the last hundredth — 0.6 never fired. Screens out crops that are facial but carry no identity, e.g. an ear at close range |
| `HAL_POSE_MODEL_PATH` | `/root/local/models/rtmpose-m.onnx` | 2D pose model path (see below) |

## Uploading / adding a model

- **Upload the face weights:** put each file in the bucket at
  `onnx_models/<file>` (matching the "Remote object" column). No code change is
  needed — the download path resolves it by basename.
- **Add a new face model:** add its `basename → onnx_models/<file>` entry to
  `_CDN_OBJECTS` in `model_store.py`, and include its path in the
  `ensure_face_models(...)` call.

## Related

- **2D pose model** (`rtmpose-m.onnx`): shares the same `/root/local/models`
  cache dir via `HAL_POSE_MODEL_PATH`, but is currently **provisioned externally**
  (device image / OTA), not auto-downloaded by HAL.
- **Cloud perception-service** has its own, separate model-download mechanism
  (`files.py` + `settings.cdn_base`) documented in
  `integrations/perception-service/docs/configuration.md` ("Model downloading").
