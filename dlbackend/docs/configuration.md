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
| `CDN_BASE` | `https://storage.googleapis.com/autonomous-models` | Where weights are fetched from |

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

### Object detectors (all opt-in)

Each detector has its own block: `OBJECT_DETECTOR__<NAME>__{ENABLED,MODEL_PATH,CLASSES_PATH,THRESHOLD}`
where `<NAME>` ∈ `YOLO_WORLD`, `YOLOE`, `OWLV2`, `GROUNDING_DINO`. All default to
`ENABLED=false`; enable the detectors you intend to call by path segment.

### Audio processor (SER / embedder front-end)

`AUDIO_EMBEDDER__*` processor toggles: `TARGET_SAMPLE_RATE`, `ENABLE_RESAMPLE`
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
| `LB__HTTP_TIMEOUT` | `120.0` | Upstream HTTP timeout (s) |
| `LB__WS_OPEN_TIMEOUT` | `120.0` | Upstream WS handshake timeout (s) |

See [crypto-and-loadbalancer.md](crypto-and-loadbalancer.md).

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
