# DL Backend — Action + Emotion + Audio Recognition

GPU-accelerated backend service for:
- real-time human action recognition via WebSocket (X3D / UniformerV2 / VideoMAE),
- facial emotion recognition via WebSocket or HTTP (POSTER V2 / EmoNet),
- speech emotion recognition via HTTP (emotion2vec_plus_large),
- optional person detection for action recognition preprocessing (YOLO12),
- speaker enrollment/recognition via HTTP APIs (AudioRecognizer).

The device streams camera frames to DL backend for action and emotion analysis, forwards end-of-utterance WAV blobs for speech emotion, and clients can register/recognize speakers through authenticated `/api/dl/audio-recognizer/*` endpoints.

## Architecture

```
Device (HAL) / Clients             Load Balancer (:7999)      DL Backend (nginx :8888 → uvicorn :8001)
┌──────────────────────┐         ┌─────────────────┐        ┌──────────────────────────────────────┐
│ Camera 640x480       │ WS/HTTP │ RSA+AES-GCM     │  HTTP  │ /api/dl/action-analysis/ws            │
│ frame_b64 every tick │────────→│ decrypt/encrypt  │───────→│ Action model (X3D/UniformerV2) ONNX  │
│                      │←────────│ round-robin proxy│←───────│ detected_classes                      │
├──────────────────────┤         └─────────────────┘        ├──────────────────────────────────────┤
│ Face crop (base64)   │  (optional encryption at LB)       │ /api/dl/emotion-recognize             │
│ from InsightFace     │────────────────────────────────────→│ Emotion model (POSTER V2/EmoNet) ONNX│
│                      │←────────────────────────────────────│ emotion + confidence                  │
├──────────────────────┤                                    ├──────────────────────────────────────┤
│ End-of-utterance WAV │   HTTP                             │ /api/dl/ser/recognize                 │
│ (same as speaker)    │────────────────────────────────────→│ SER model (emotion2vec) ONNX         │
│                      │←────────────────────────────────────│ label + confidence (9-class)          │
├──────────────────────┤                                    ├──────────────────────────────────────┤
│ App / tools          │   HTTP                             │ /api/dl/audio-recognizer/*            │
│ wav URL/chunks/PCM16 │────────────────────────────────────→│ register/recognize/list/remove        │
└──────────────────────┘                                    └──────────────────────────────────────┘
```

## Models

### Action Recognition

Selectable via `ACTION_RECOGNITION_MODEL` env var:

| Model | Enum | ONNX file | Input | Default frames |
|---|---|---|---|---|
| **X3D** (default) | `x3d` | `x3d_m_16x5x1_int8.onnx` | 256×256 | 16 |
| **UniformerV2** | `uniformerv2` | User-provided | 224×224 | 8 |
| **VideoMAE** | `videomae` | `videomae_int8.onnx` | 224×224 | 16 |

All classify from **Kinetics-400** action classes, filtered by a configurable whitelist (`white_list.txt`).

### Emotion Recognition

Selectable via `EMOTION_RECOGNITION_MODEL` env var:

| Model | Enum | ONNX file | Input | Output |
|---|---|---|---|---|
| **POSTER V2** (default) | `posterv2` | `posterv2_7cls.onnx` | 224×224, ImageNet norm | 7 emotions (RAF-DB: Surprise, Fear, Disgust, Happy, Sad, Anger, Neutral) |
| **EmoNet-8** | `emonet_8` | `emonet_8.onnx` | 256×256 | 8 emotions (Neutral, Happy, Sad, Surprise, Fear, Disgust, Anger, Contempt) + valence + arousal |
| **EmoNet-5** | `emonet_5` | `emonet_5.onnx` | 256×256 | 5 emotions (Neutral, Happy, Sad, Surprise, Anger) + valence + arousal |

Face detection for emotion uses **YuNet** (`face_detection_yunet_2023mar.onnx`) to crop faces before classification. This is separate from HAL's InsightFace (used for identity recognition on-device).

### Speech Emotion Recognition (SER)

Selectable via `SER_RECOGNITION_MODEL` env var:

| Model | Enum | ONNX file | Input | Output |
|---|---|---|---|---|
| **emotion2vec_plus_large** (default) | `emotion2vec_plus_large` | exported from FunASR snapshot on cold start | mono 16 kHz waveform | 9 classes (angry, disgusted, fearful, happy, neutral, other, sad, surprised, `<unk>`) + softmax confidence |

The engine loads once at startup. Cold-start path: if no `.onnx` is cached locally, the engine downloads the FunASR checkpoint, exports ONNX into `models/<engine>/emotion2vec.onnx`, and writes `labels.txt` from the snapshot's `tokens.txt`. After the first build, only `onnxruntime` is needed at serve time — `torch` and `funasr` can be uninstalled.

HAL's `SpeechEmotionService` is the only known caller in this monorepo. After each mic session ends (independent of STT transcript), `VoiceService._submit_speech_emotion_from_session` builds a mono 16 kHz WAV from the session buffer and POSTs it to `/api/dl/ser/recognize`. Speaker recognition is invoked inline to populate the `user` field but does not gate SER.

### Person Detection (Optional)

When enabled, YOLO12 detects the largest person in each frame and crops it before feeding to the action recognition model. Helps when the camera is moving (servo ego-motion).

| Setting | Default |
|---|---|
| Model | `yolo12x.pt` (Ultralytics) |
| Enabled | `false` |
| Confidence threshold | 0.4 |
| Bbox expand scale | 2.0 (expands crop around detected person) |

### Speaker Recognition

Selectable via `AUDIO_RECOGNIZER_ENGINE` env var:

| Model | Enum | ONNX file | Embedding dim |
|---|---|---|---|
| **WeSpeaker ResNet34** (default) | `resnet34` | `voxceleb_resnet34_LM.onnx` | 256 |
| ECAPA-TDNN 1024 | `ecapa-tdnn1024` | `voxceleb_ECAPA1024_LM.onnx` | — |
| CAM++ | `campplus` | `voxceleb_CAM++.onnx` | — |

## API Endpoints

### Action Analysis (WebSocket)

```
WS /api/dl/action-analysis/ws
```

**Client → Server:**
```json
{"type": "config", "whitelist": ["reading", "walking", "using computer"], "threshold": 0.3}
{"type": "frame", "frame_b64": "<base64 JPEG>"}
```

**Server → Client:**
```json
{"detected_classes": [["using computer", 0.72], ["texting", 0.35]]}
```

### Emotion Analysis (WebSocket)

```
WS /api/dl/emotion-analysis/ws
```

**Client → Server:**
```json
{"type": "frame", "task": "emotion", "frame_b64": "<base64 JPEG>"}
{"type": "config", "task": "emotion", "threshold": 0.5}
```

**Server → Client:**
```json
{"detections": [{"emotion": "Happy", "confidence": 0.82, "face_confidence": 0.95, "bbox": [x,y,w,h], "valence": null, "arousal": null}]}
```

### Emotion Recognition (HTTP)

```
POST /api/dl/emotion-recognize
```

**Request:**
```json
{"image_b64": "<base64 face crop>", "threshold": 0.5}
```

**Response:**
```json
{"detections": [{"emotion": "Happy", "confidence": 0.82, "face_confidence": 1.0, "bbox": [0,0,W,H]}]}
```

> **Note:** HAL currently uses the HTTP endpoint (not WebSocket) for emotion. Face crops are produced by InsightFace on-device, then sent to dlbackend for emotion classification only.

### Speech Emotion Recognition (HTTP)

```
POST /api/dl/ser/recognize
GET  /api/dl/ser/labels
```

Three accepted body formats (`multipart/form-data` upload, base64 JSON, or remote URL JSON). HAL uses the base64 JSON form so it can reuse the WAV bytes already in memory from speaker ID:

**Request (base64):**
```json
{"audio_b64": "<base64 WAV (mono 16 kHz)>", "return_scores": false}
```

**Response:**
```json
{"label": "happy", "confidence": 0.9981, "scores": null}
```

`scores` is `null` when `return_scores=false`; otherwise it's the full per-label softmax map. Error codes: `400` (bad body / audio), `401`/`403` (api key), `503` (engine init failed). See `dlbackend/src/core/ser/README.md` for the full SER spec.

### Audio Recognition (HTTP)

Base path: `/api/dl/audio-recognizer`

| Method | Path | Description |
|---|---|---|
| POST | `/register` | Enroll speaker (wav_path/chunks/pcm16_b64/multipart) |
| POST | `/recognize` | Identify speaker from audio |
| GET | `/speakers` | List enrolled speakers |
| DELETE | `/speakers/{name}` | Remove speaker |

### Health Check

```
GET /api/dl/health
→ {"status": "ok", "action_model": true, "emotion_model": true}
```

## Data Flows

### Action Analysis

1. **Pi**: `SensingService._tick()` reads camera frame every 2s
2. **Pi**: `MotionPerception` → `RemoteMotionChecker.update()` encodes frame to base64 JPEG
3. **WebSocket**: `{"type": "frame", "frame_b64": "..."}` sent to RunPod with `X-API-Key` header
4. **RunPod**: Action model buffers frames, runs inference every `frame_interval`
5. **RunPod**: If person detector enabled → crop largest person first, then classify
6. **RunPod**: Preprocesses (BGR→RGB, center crop, normalization), runs softmax over whitelisted actions
7. **WebSocket**: Returns `{"detected_classes": [["walking", 0.87], ["reading book", 0.42]]}`
8. **Pi**: Buffers actions + snapshots for `MOTION_FLUSH_S`, then sends aggregated event
9. **Device → Lamp**: `POST /api/sensing/event` with `type: "motion.activity"` or `type: "motion"`
10. **Lamp → OpenClaw**: Agent receives event, responds based on detected activity

### Emotion Analysis

1. **Pi**: `SensingService._tick()` detects faces via InsightFace (on-device)
2. **Pi**: `EmotionPerception` crops face, encodes to base64 JPEG
3. **HTTP**: `POST /api/dl/emotion-recognize` with face crop + threshold
4. **RunPod**: YuNet re-detects face in crop (optional), POSTER V2 / EmoNet-8 / EmoNet-5 classifies emotion
5. **HTTP**: Returns `{"detections": [{"emotion": "Happy", "confidence": 0.82}]}`
6. **Pi**: Buffers, applies polarity-bucket dedup, fires `emotion.detected` event
7. **Device → Lamp**: `POST /api/sensing/event` with `type: "emotion.detected"`

### Speech Emotion Recognition

1. **Pi**: Mic session ends (VAD trigger → ~2.5 s silence); `_submit_speech_emotion_from_session` runs `_session_wav_for_ser` + inline `_identify_and_decorate` (for `user` only) + `SpeechEmotionService.submit`. Independent of STT — fires even when transcript is empty (laughter, sighs).
2. **Pi**: `user` is the enrolled speaker label on match, otherwise `unknown` (including when speaker recognize fails but audio is long enough)
3. **Pi worker thread**: `POST /api/dl/ser/recognize` with `{"audio_b64": "...", "return_scores": false}`
4. **RunPod**: `emotion2vec_plus_large` runs softmax over 9 classes
5. **HTTP**: Returns `{"label": "sad", "confidence": 0.72}`
6. **Pi**: Buffers per user, every `SPEECH_EMOTION_FLUSH_S` (default 10 s) computes mode + bucket, applies `(user, bucket)` TTL dedup
7. **Device → Lamp**: `POST /api/sensing/event` with `type: "speech_emotion.detected"` and `current_user` set

## Configuration

### RunPod (.env)

```env
DL_API_KEY=<shared secret>

# Action recognition: x3d | uniformerv2 | videomae
ACTION_RECOGNITION_MODEL=x3d
# ACTION_RECOGNITION_CKPT_PATH=/path/to/model.onnx

# Emotion recognition: posterv2 | emonet_8 | emonet_5
EMOTION_RECOGNITION_MODEL=posterv2
# EMOTION_RECOGNITION_CKPT_PATH=/path/to/posterv2_7cls.onnx

# Per-model overrides (nested via __ delimiter)
# X3D__CONFIDENCE_THRESHOLD=0.3
# X3D__MAX_FRAMES=16
# X3D__W=256
# X3D__H=256
# UNIFORMERV2__CONFIDENCE_THRESHOLD=0.3
# UNIFORMERV2__MAX_FRAMES=8
# EMOTION__CONFIDENCE_THRESHOLD=0.5
# EMOTION__FRAME_INTERVAL=1.0

# Speech emotion recognition: emotion2vec_plus_large (default)
# SER_RECOGNITION_MODEL=emotion2vec_plus_large
# SER_RECOGNITION_CKPT_PATH=/abs/path/emotion2vec.onnx
# SER_RECOGNITION_LABELS_PATH=/abs/path/labels.txt
# SER__SAMPLE_RATE=16000
# SER__INTRA_OP_THREADS=8
# SER__PROVIDERS=                 # empty = auto-detect cuda → coreml → cpu

# Person detector (crops person before action recognition)
# PERSON_DETECTOR__ENABLED=false
# PERSON_DETECTOR__MODEL_NAME=yolo12x.pt
# PERSON_DETECTOR__CONFIDENCE_THRESHOLD=0.4
# PERSON_DETECTOR__BBOX_EXPAND_SCALE=2.0

# Audio recognition: resnet34 | ecapa-tdnn1024 | campplus
AUDIO_RECOGNIZER_ENGINE=resnet34
```

### Pi (.env)

```env
DL_BACKEND_URL=wss://<POD_ID>-8888.proxy.runpod.net/lelamp/api/dl/action-analysis/ws
DL_API_KEY=<shared secret>
HAL_MOTION_ENABLED=true
```

### Thresholds (os/hal/config.py)

| Parameter | Default | Purpose |
|---|---|---|
| `MOTION_CONFIDENCE_THRESHOLD` | 0.3 | Min action confidence score |
| `MOTION_FLUSH_S` | 10.0 | Buffer flush interval (seconds) |
| `MOTION_EVENT_COOLDOWN_S` | 360.0 | Event cooldown to avoid spam (6 min) |
| `EMOTION_CONFIDENCE_THRESHOLD` | configurable | Min facial emotion confidence to fire event |
| `SPEECH_EMOTION_ENABLED` | `true` | Master kill switch for speech emotion |
| `SPEECH_EMOTION_CONFIDENCE_THRESHOLD` | 0.5 | Min SER confidence to buffer |
| `SPEECH_EMOTION_FLUSH_S` | 10.0 | Per-user buffer drain cadence |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | 300.0 | `(user, bucket)` TTL (5 min) |
| `SPEECH_EMOTION_MIN_AUDIO_S` | 0.8 | Min utterance length |
| `DL_SER_ENDPOINT` | `/lelamp/api/dl/ser/recognize` | Path suffix on `DL_BACKEND_URL` (URL path, not repo path) |
| `DL_ENCRYPTION_ENABLED` | `false` | Enable client-side encryption for DL backend |
| `DL_ENCRYPTION_REQUIRED` | `false` | Fail if encryption setup fails (no plaintext fallback) |

## Key Files

### dlbackend/

| File | Purpose |
|---|---|
| `src/server.py` | FastAPI app, model loading, WS + HTTP routes, health check |
| `src/config.py` | Pydantic settings: model selection, per-model configs, person detector |
| `src/enums/action_recognizer.py` | `HumanActionRecognizerEnum` (x3d/uniformerv2/videomae) |
| `src/enums/emotion_recognizer.py` | `EmotionRecognizerEnum` (posterv2/emonet_8/emonet_5) |
| `src/enums/person_detector.py` | `PersonDetectorEnum` (yolo) |
| `src/core/action/base.py` | Base action recognizer model + session (ONNX, frame buffer, predict) |
| `src/core/action/x3d.py` | X3D model (256×256, 16 frames) |
| `src/core/action/uniformerv2.py` | UniformerV2 model (224×224, 8 frames) |
| `src/core/action/videomae.py` | VideoMAE model (224×224, 16 frames) |
| `src/core/persondetector/base.py` | Abstract `PersonDetector` base class |
| `src/core/persondetector/yolo.py` | YOLO person detector (optional action preprocessing) |
| `src/core/emotion/emotion.py` | EmotionModel — YuNet + classifier, session management |
| `src/core/emotion/utils.py` | Factory: selects PosterV2 / EmoNet-8 / EmoNet-5 based on config |
| `src/core/emotion/recognizer/posterv2.py` | POSTER V2 classifier (224×224, 7 RAF-DB classes, ImageNet norm) |
| `src/core/emotion/recognizer/emonet.py` | EmoNet classifier (256×256, 5 or 8 classes + valence/arousal) |
| `src/core/emotion/recognizer/base.py` | Abstract `EmotionRecognizer` base class |
| `src/core/faces/yunet.py` | YuNet face detector (for emotion pipeline face cropping) |
| `src/core/ser/speech_emotion_recognizer/base.py` | Abstract SER base class + label dispatch |
| `src/core/ser/speech_emotion_recognizer/emotion2vec.py` | emotion2vec_plus_large concrete engine (ONNX) |
| `src/core/ser/speech_emotion_recognizer/factory.py` | `create_speech_emotion_recognizer()` selector |
| `src/core/ser/prepare_onnx.py` | Cold-start FunASR → ONNX export fallback |
| `src/protocols/htpp/ser.py` | `/api/dl/ser/recognize` + `/api/dl/ser/labels` routes |
| `src/core/audio_recognition/audio_recognizer.py` | Speaker embedding (WeSpeaker ResNet34 / ECAPA / CAM++) |
| `src/core/audio_recognition/speaker_db.py` | JSON-backed speaker storage |
| `src/core/models.py` | Pydantic schemas: ActionResponse, EmotionDetection, EmotionResponse, PersonDetection |
| `src/core/crypto/rsa_aes.py` | RSA+AES-256-GCM hybrid encryption (`RSAAESCrypto`, `AESGCMSession`) |
| `src/core/models/crypto.py` | Raw payload dataclasses for crypto (`AESGCMCipherPayload`, `RSAAESCipherPayload`, etc.) |
| `src/lbserver/app.py` | Load balancer — round-robin HTTP/WS proxy with encryption |
| `src/lbserver/models.py` | Pydantic wire-format models (`CipherHTTPRequest`, `WSCipherMessage`, etc.) |
| `src/lbserver/utils/crypto.py` | HTTP decrypt/encrypt helpers for the LB proxy |
| `nginx.conf` | Reverse proxy :8888 → :8001, `/lelamp/` prefix strip (URL path), WS upgrade |
| `Dockerfile` | CUDA 12.4 PyTorch + nginx + uvicorn |
| `start.sh` | RunPod startup: nginx + uvicorn |

### os/hal/ (device side)

| File | Purpose |
|---|---|
| `service/sensing/perceptions/processors/motion.py` | `RemoteMotionChecker` — WS client, frame encoding, action buffering |
| `service/sensing/perceptions/processors/emotion.py` | `RemoteEmotionRecognizer` — HTTP client, face crop → emotion classify |
| `service/sensing/crypto.py` | Client-side `CryptoSession`, wire-format models, public key resolution |
| `service/voice/speech_emotion/service.py` | `SpeechEmotionService` — queue + worker + flush + dedup + Lamp POST |
| `service/voice/speech_emotion/emotion2vec.py` | `Emotion2VecRecognizer` — HTTP client to `/api/dl/ser/recognize` |
| `service/voice/speech_emotion/base.py` | `BaseSpeechEmotionRecognizer` ABC + `SpeechEmotionResult` |
| `service/voice/speech_emotion/utils.py` | Bucketing + hedged message formatting |
| `service/voice/speech_emotion/constants.py` | Label vocabulary, bucket map, event type, defaults |
| `config.py` | `DL_BACKEND_URL`, `DL_API_KEY`, thresholds, `SPEECH_EMOTION_*` knobs |
| `service/sensing/sensing_service.py` | Orchestrates all perceptions in `_tick()` |

## Nginx Routing

```
:8888 (public)
├── /              → 127.0.0.1:8000
└── /lelamp/       → 127.0.0.1:8001  (strips /lelamp/ prefix, WS upgrade enabled)
```

All device traffic goes through `/lelamp/` → port 8001 (FastAPI). Routes are prefixed `/api/dl/` on the FastAPI side.

Full URL examples:
```
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/action-analysis/ws
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/emotion-recognize
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/emotion-analysis/ws
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/ser/recognize
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/audio-recognizer/register
https://<POD>-8888.proxy.runpod.net/lelamp/api/dl/health
```

## Authentication

- HTTP routes under `/api/dl/*` use header `X-API-Key` when `DL_API_KEY` is set.
- WebSocket endpoints validate `X-API-Key` from WS headers on connect.
- If `DL_API_KEY` is empty, auth is effectively disabled (dev mode).

## Encryption (RSA + AES-256-GCM)

Optional hybrid encryption handled at the **load balancer** layer. DL server (dlserver) stays plaintext — the LB decrypts inbound requests and encrypts outbound responses transparently.

### Architecture

```
HAL (client)                             Load Balancer (:7999)                    DL Server (:8001)
┌──────────────┐   encrypted traffic    ┌──────────────────────┐   plaintext     ┌──────────────┐
│ CryptoSession │ ────────────────────→ │ RSAAESCrypto         │ ─────────────→  │ FastAPI       │
│ (AES-256-GCM) │ ←──────────────────── │ decrypt → forward    │ ←─────────────  │ (no crypto)   │
└──────────────┘                        │ encrypt ← response   │                 └──────────────┘
                                        └──────────────────────┘
```

### Crypto Primitives

| Component | Algorithm | Details |
|---|---|---|
| Key exchange | RSA-OAEP (SHA-256) | Client encrypts a random 32-byte AES session key with the LB's RSA public key |
| Data encryption | AES-256-GCM | 12-byte nonce, authenticated (tag embedded in cipher_data) |
| Key pair | RSA 2048-bit (configurable) | Generated at startup; persisted to disk if `CRYPTO__KEY_DIR` is set |

### Public Key Endpoint

```
GET /api/crypto/public-key
→ PEM-encoded RSA public key (text/plain)
→ 404 if crypto is disabled
```

HAL fetches this at startup to encrypt session keys. Alternatively, the key can be loaded from a local PEM file via `DL_PUBLIC_KEY_FILE` (skips the fetch).

### HTTP Encryption

When crypto is enabled and the request body matches the `CipherHTTPRequest` schema, the LB decrypts before forwarding and encrypts the response.

**Client → LB (encrypted request):**
```json
{
  "encrypted_key": "<base64 RSA-OAEP(AES session key)>",
  "nonce": "<base64 12-byte GCM nonce>",
  "cipher_data": "<base64 AES-GCM(plaintext + tag)>"
}
```

**LB → Client (encrypted response):**
```json
{
  "nonce": "<base64 12-byte GCM nonce>",
  "cipher_data": "<base64 AES-GCM(plaintext + tag)>"
}
```

Plain (non-encrypted) requests pass through unchanged when `CRYPTO__REQUIRE_ENCRYPTION=false`.

### WebSocket Encryption

After connecting, the client performs a key exchange before sending frames:

**1. Key exchange (client → LB):**
```json
{"type": "key_exchange", "encrypted_key": "<base64 RSA-OAEP(AES session key)>"}
```

**2. Key exchange response (LB → client):**
```json
{"status": "key_exchange_ok"}
```

**3. All subsequent messages use `WSCipherMessage`:**
```json
{"type": "encrypted", "nonce": "<base64>", "cipher_data": "<base64>"}
```

If key exchange is skipped and `CRYPTO__REQUIRE_ENCRYPTION=false`, messages pass through as plaintext. If `CRYPTO__REQUIRE_ENCRYPTION=true`, the LB closes the connection with code 1008.

### Configuration

#### Load Balancer (dlbackend/.env)

| Variable | Default | Description |
|---|---|---|
| `CRYPTO__ENABLED` | `true` | Enable encryption at the LB |
| `CRYPTO__KEY_DIR` | `None` | Directory to persist RSA keys (omit for in-memory) |
| `CRYPTO__KEY_SIZE` | `2048` | RSA key size in bits |
| `CRYPTO__REQUIRE_ENCRYPTION` | `false` | Reject plaintext requests/connections |

#### HAL Client (os/hal/.env)

| Variable | Default | Description |
|---|---|---|
| `HAL_DL_ENCRYPTION` | `false` | Enable client-side encryption |
| `HAL_DL_ENCRYPTION_REQUIRED` | `false` | Fail if encryption setup fails (no plaintext fallback) |
| `DL_PUBLIC_KEY_FILE` | _(empty)_ | Path to RSA public key PEM file (skips fetch from LB if set) |
| `DL_PUBLIC_KEY_ENDPOINT` | `/crypto/public-key` | Path appended to `DL_BACKEND_URL` to fetch the public key |

### Key Files

| File | Purpose |
|---|---|
| `dlbackend/src/core/crypto/rsa_aes.py` | `RSAAESCrypto` (server-side RSA+AES), `AESGCMSession` |
| `dlbackend/src/core/models/crypto.py` | Raw payload dataclasses (`AESGCMCipherPayload`, `AESGCMPlainPayload`, `RSAAESCipherPayload`, `RSAAESPlainPayload`) |
| `dlbackend/src/lbserver/models.py` | Pydantic wire-format models (`CipherHTTPRequest`, `CipherHTTPResponse`, `WSKeyExchangeRequest`, `WSCipherMessage`) |
| `dlbackend/src/lbserver/utils/crypto.py` | HTTP decrypt/encrypt helpers (`try_decrypt_http_body`, `encrypt_http_response`) |
| `dlbackend/src/lbserver/app.py` | LB integration (HTTP proxy + WS proxy with crypto) |
| `os/hal/service/sensing/crypto.py` | Client-side `CryptoSession`, wire-format models, public key resolution |

## Deployment

### RunPod

```bash
cd /workspace/ai-lamp-openclaw/dlbackend
bash start.sh
```

### Docker

```bash
cd dlbackend
docker build -t dlbackend .
docker run --gpus all -p 8888:8888 dlbackend
```

## Event Types Produced

| Event | When | Sent to Agent |
|---|---|---|
| `motion.activity` | Person present + actions detected | Yes, with action list |
| `motion` | Large motion, no known person | Yes, "someone may have entered" |
| `emotion.detected` | Face emotion above confidence threshold | Yes, with emotion label + confidence |

## Error Handling

- **WebSocket disconnect**: `RemoteMotionChecker` catches `ConnectionClosedError`, reconnects on next tick
- **RunPod unavailable**: `DL_BACKEND_URL` empty → perception not created, silently skipped
- **Model not loaded**: Server returns WebSocket close with "Model not ready" / HTTP 503
- **Bad frame**: Server logs warning, skips frame, continues
- **Audio multipart without file**: `422` with validation message
- **Audio URL not http/https**: validation error (local filesystem paths are rejected)
- **Audio model/dependency unavailable**: audio endpoints return `503` from protocol layer
