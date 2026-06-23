# Configuration

`dlserver` and `lbserver` read settings with `pydantic-settings` from the
environment / `.env`. Nested settings use a `__` delimiter
(e.g. `ACTION__MODEL=x3d`). See `src/config.py` and `dlbackend/.env.example`.

## Core

| Env var | Default | Meaning |
|---------|---------|---------|
| `DL_API_KEY` | _(required)_ | Shared API key; `dlserver` raises at startup if unset |
| `CACHE_DIR` | `~/.cache/dlbackend` | Root cache dir |
| `MODEL_CACHE_DIR` | `~/.cache/dlbackend/models` | Downloaded model weights |
| `CDN_BASE` | `https://storage.googleapis.com/autonomous-models` | Public bucket weights are fetched from (see [Model downloading](#model-downloading)) |

## Model downloading

Model weights are **not** committed to the repo. Each perception downloads its
weights on first load and caches them, so the binary stays small and a fresh
checkout pulls only the models it actually uses.

How it works (`src/core/utils/files.py`, `ensure_downloaded`):

1. The predictor resolves a local path under `MODEL_CACHE_DIR`
   (default `~/.cache/dlbackend/models/<filename>`).
2. If the file already exists → use it, no network.
3. If missing → download from the resolved `remote`:
   - an `http(s)` URL → direct download (atomic: `.part` temp → `replace`);
   - otherwise treated as a **HuggingFace repo id** → `huggingface_hub.hf_hub_download`.

The default `remote` is `CDN_BASE` + the model's path. Downloads are lazy (only
when a perception is first used) and cached, so subsequent runs don't re-fetch.

### Public weights (Google Cloud Storage)

Base URL: `https://storage.googleapis.com/autonomous-models/`

| Model | Download URL |
|-------|--------------|
| X3D (action) | `…/onnx_models/x3d_m_16x5x1_int8.onnx` ⚠️ |
| VideoMAE (action) | `…/onnx_models/videomae_fp32.onnx` |
| UniformerV2 (action) | `…/onnx_models/uniformerv2-l-224-k400_fp32.onnx` |
| POSTER V2 (FER) | `…/onnx_models/posterv2_7cls.onnx` |
| EmoNet-8 (FER) | `…/onnx_models/emonet_8.onnx` |
| EmoNet-5 (FER) | `…/onnx_models/emonet_5.onnx` ⚠️ |
| emotion2vec (SER) | `…/onnx_models/emotion2vec.onnx` |
| RTMPose-m (pose 2D) | `…/onnx_models/rtmpose-m.onnx` |
| TCPFormer (pose 3D) | `…/onnx_models/tcpformer_h36m_243.onnx` |
| WeSpeaker ResNet34 (embed) | `…/onnx_models/wespeaker_resnet34.onnx` ⚠️ |
| WeSpeaker ECAPA-1024 (embed) | `…/onnx_models/wespeaker_ecapa_tdnn1024.onnx` |
| WeSpeaker CAM++ (embed) | `…/onnx_models/wespeaker_campplus.onnx` ⚠️ |
| YuNet (face) | `…/onnx_models/face_detection_yunet_2023mar.onnx` |
| YOLO person (PyTorch) | `…/pytorch_models/yolo12x.pt` |
| YOLO person (ONNX) | `…/onnx_models/yolo12x_raw.onnx`, `…/onnx_models/yolo12x.onnx` |
| YOLO-World (PyTorch) | `…/pytorch_models/yolov8x-worldv2.pt` |
| YOLO-World (ONNX) | `…/onnx_models/yolov8x-worldv2_raw.onnx`, `…/onnx_models/yolov8x-worldv2.onnx` |
| OWLv2 (ONNX) | `…/onnx_models/owlv2_raw.onnx`, `…/onnx_models/owlv2.onnx` |

(`…` = the base URL above. The mapping lives in `CDN_PATHS` in
`src/core/utils/files.py` — keep this table in sync with it.)

> ⚠️ **Availability (verified against the public bucket on 2026-06-16):** the four
> rows marked ⚠️ are **not currently present** in the bucket (HTTP `404`) — including
> the default action model **X3D** and the default audio embedder **WeSpeaker
> ResNet34**, so a fresh out-of-the-box run will fail to download them. The other 11
> files return `200`. Until the weights are uploaded (or `CDN_PATHS` in
> `src/core/utils/files.py` is corrected to the real filenames — pending confirmation
> from the maintainers), work around it by either selecting a model whose weights do
> exist (e.g. `ACTION__MODEL=videomae`, `AUDIO_EMBEDDER__MODEL=ecapa-tdnn1024`) or
> pointing the model at a local file / alternate source via `<NAME>__CKPT_PATH` or
> `<NAME>__REMOTE_URL`.

Object detector **OWLv2** has both ONNX (default, from CDN) and HuggingFace
(`google/owlv2-large-patch14-ensemble`) backends. The ONNX backend is used when
`USE_ONNX=true` (the default).

### Notes for self-hosting / forks

- The bucket objects must be **public-read** for the unauthenticated runtime
  download to work; a private bucket returns `403`. You can fetch any URL above
  directly with `curl`/`wget` to pre-seed the cache.
- To host your own weights, set `CDN_BASE` to your bucket/CDN and mirror the same
  `onnx_models/` + `pytorch_models/` layout.
- To use a specific local file (air-gapped or custom-trained), set the per-model
  `<NAME>__CKPT_PATH`, or override the source with `<NAME>__REMOTE_URL` (a URL or a
  HuggingFace repo id).

## Perceptions

Each perception has an `ENABLED` flag and a `MODEL` selector; most also accept a
checkpoint override and threshold(s).

| Env var | Default | Meaning |
|---------|---------|---------|
| `ACTION__ENABLED` | `true` | Enable action recognition |
| `ACTION__MODEL` | `x3d` | `x3d` \| `uniformerv2` \| `videomae` |
| `ACTION__CKPT_PATH` | _(auto)_ | Local ONNX override |
| `ACTION__CONFIDENCE_THRESHOLD` | _(none)_ | Min action score |
| `ACTION__MAX_FRAMES`, `ACTION__FRAME_INTERVAL`, `ACTION__W`, `ACTION__H`, `ACTION__BATCH_SIZE` | per-model | Clip/inference shape |
| `FER__ENABLED` | `true` | Enable facial emotion |
| `FER__MODEL` | `posterv2` | `posterv2` \| `emonet_8` \| `emonet_5` |
| `FER__CONFIDENCE_THRESHOLD`, `FER__FRAME_INTERVAL`, `FER__CKPT_PATH`, `FER__BATCH_SIZE` | per-model | |
| `SER__ENABLED` | `true` | Enable speech emotion |
| `SER__MODEL` | `emotion2vec` | SER engine |
| `SER__CKPT_PATH`, `SER__LABELS_PATH`, `SER__BATCH_SIZE` | _(auto)_ | Overrides |
| `POSE__ENABLED` | `true` | Enable pose estimation |
| `POSE__MODEL` | `rtmpose` | 2D estimator |
| `POSE__LIFTER_3D` | `tcpformer` | 3D lifter (set empty to disable 3D) |
| `POSE__ERGO_ASSESSOR` | `rula` | Ergonomics assessor (empty to disable) |
| `POSE__CONFIDENCE_THRESHOLD_2D`, `POSE__ERGO_CONFIDENCE_THRESHOLD` | _(none)_ | Thresholds |
| `PERSON_DETECTOR__ENABLED` | `false` | Crop person before action recognition |
| `PERSON_DETECTOR__MODEL` | `yolo` | Person detector |
| `PERSON_DETECTOR__MODEL_NAME` | `yolo12x.pt` | Weights |
| `PERSON_DETECTOR__CONFIDENCE_THRESHOLD` | `0.4` | |
| `PERSON_DETECTOR__BBOX_EXPAND_SCALE` | `2.0` | Expand crop around person |
| `PERSON_DETECTOR__MIN_AREA_RATIO` | `0.25` | Min person/frame area to use |
| `AUDIO_EMBEDDER__ENABLED` | `false` | Enable speaker embedder |
| `AUDIO_EMBEDDER__MODEL` | `resnet34` | `resnet34` \| `ecapa-tdnn1024` \| `campplus` |
| `AUDIO_EMBEDDER__MODEL_PATH` | _(auto)_ | Local embedder weights override path |
| `AUDIO_EMBEDDER__REMOTE_URL` | _(auto)_ | Alternate remote URL or HuggingFace repo id |
| `AUDIO_EMBEDDER__BATCH_SIZE` | _(auto)_ | Inference batch size |

### Object detectors (all opt-in)

Each detector has its own block: `OBJECT_DETECTOR__<NAME>__{ENABLED,USE_ONNX,MODEL_PATH,REMOTE_URL,CLASSES_PATH,THRESHOLD}`
where `<NAME>` ∈ `YOLO_WORLD`, `OWLV2`. All default to
`ENABLED=false`; enable the detectors you intend to call by path segment.

| Env var | Default | Meaning |
|---------|---------|---------|
| `OBJECT_DETECTOR__<NAME>__ENABLED` | `false` | Enable this detector |
| `OBJECT_DETECTOR__<NAME>__USE_ONNX` | `true` | Use ONNX Runtime backend (when `false`, falls back to PyTorch/HF) |
| `OBJECT_DETECTOR__<NAME>__MODEL_PATH` | _(auto)_ | Local model weights override |
| `OBJECT_DETECTOR__<NAME>__REMOTE_URL` | _(auto)_ | Custom download URL (overrides CDN default) |
| `OBJECT_DETECTOR__<NAME>__CLASSES_PATH` | _(none)_ | Default class list file |
| `OBJECT_DETECTOR__<NAME>__THRESHOLD` | per-model | Confidence threshold |
| `OBJECT_DETECTOR__<NAME>__BATCH_SIZE` | _(auto)_ | Inference batch size |

When `USE_ONNX=true` (the default), ONNX predictors resolve their model path and
remote URL from `ModelEnum` entries and auto-download via `ensure_downloaded` on
first use. Models are cached in `MODEL_CACHE_DIR` (`~/.cache/dlbackend/models/` by
default). No manual download step is needed.

### Audio processor (SER / embedder front-end)

`AUDIO_EMBEDDER__PROCESSOR__*` toggles (e.g. `AUDIO_EMBEDDER__PROCESSOR__TARGET_SAMPLE_RATE`): `TARGET_SAMPLE_RATE`, `ENABLE_RESAMPLE`
(`true`), `ENABLE_HIGH_PASS` (`true`), `HIGH_PASS_CUTOFF_HZ`, `ENABLE_NOISE_REDUCE`
(`true`), `NOISE_REDUCE_STATIONARY` (`false`), `ENABLE_VAD` (`true`),
`VAD_MIN_DURATION_SEC`, `VAD_MIN_VOICE_RATIO`, `ENABLE_RMS_NORMALIZE` (`true`),
`RMS_TARGET`.

## Crypto & load balancer (lbserver)

| Env var | Default | Meaning |
|---------|---------|---------|
| `CRYPTO__ENABLED` | `true` | Enable LB encryption |
| `CRYPTO__KEY_DIR` | `~/.dlbackend/keys` | RSA key persistence |
| `CRYPTO__KEY_SIZE` | `2048` | RSA key size (bits) |
| `CRYPTO__REQUIRE_ENCRYPTION` | `false` | Reject plaintext |
| `LB__BACKENDS` | `""` | Comma-separated dlserver URLs |
| `LB__PORT` | `7999` | lbserver port |
| `LB__HOST` | `0.0.0.0` | lbserver host |
| `LB__INTERNAL_PREFIX` | `""` | Path prefix prepended to the upstream URL |
| `LB__HTTP_TIMEOUT` | `120.0` | Upstream HTTP timeout (s) |
| `LB__WS_OPEN_TIMEOUT` | `120.0` | Upstream WS handshake timeout (s) |

See [crypto-and-loadbalancer.md](crypto-and-loadbalancer.md) for the proxy/scaling
topology and [deployment.md](deployment.md) for how to run it.

## HAL client (`os/hal`)

The device side that calls this backend reads its own env (see `os/hal/config.py`).
Key knobs:

| Env var | Default | Meaning |
|---------|---------|---------|
| `DL_BACKEND_URL` | _(empty)_ | Base URL; empty disables remote perception entirely |
| `DL_API_KEY` | _(empty)_ | Must match the backend's `DL_API_KEY` |
| `HAL_DL_ENCRYPTION` | `false` | Enable client-side RSA/AES |
| `HAL_DL_ENCRYPTION_REQUIRED` | `false` | Fail if encryption setup fails (no plaintext fallback) |
| `DL_PUBLIC_KEY_FILE` | _(empty)_ | Load RSA public key from PEM instead of fetching |
| `DL_PUBLIC_KEY_ENDPOINT` | `/crypto/public-key` | Path appended to `DL_BACKEND_URL` |

> HAL field names are owned by `os/hal/config.py` — treat that file as source of
> truth and update this table if they drift.
</content>
