# API Reference

Paths below are the **internal `dlserver` routes**. Through the nginx front door
the device prefix `/lelamp/` maps onto `/hal/` (see [architecture.md](architecture.md)),
so e.g. `/hal/api/dl/health` is reached externally as `/lelamp/api/dl/health`.

## Authentication

Every endpoint requires header `X-API-Key: <DL_API_KEY>`.

- HTTP: enforced by a `verify_api_key` dependency → `401` on mismatch.
- WebSocket: validated on the handshake (`verify_ws_api_key`); the socket is
  rejected before any frame is processed.
- `dlserver` raises at startup if `DL_API_KEY` is unset (no implicit dev bypass).

Image inputs are base64 JPEG/PNG (decoded with `cv2.imdecode`). Audio inputs are
base64 WAV (decoded with `soundfile`).

## WebSocket message convention

The streaming endpoints (action, emotion, pose, object) share a message protocol
discriminated by a `type` field, with a `task` tag per perception:

| `type` | Direction | Purpose |
|--------|-----------|---------|
| `config` | client → server | Set thresholds / whitelist / detector options |
| `frame` | client → server | Send one base64 frame to analyze |
| `heartbeat` | client → server | Keep-alive |

The server replies with the perception's result object (shapes below).

---

## Action analysis — WebSocket

```
WS /hal/api/dl/action-analysis/ws
```

Client → server:

```json
{"type": "config", "task": "action", "whitelist": ["walking", "reading"], "threshold": 0.3,
 "person_detection_enabled": false, "person_min_area_ratio": 0.25}
{"type": "frame", "task": "action", "frame_b64": "<base64 JPEG>"}
{"type": "heartbeat", "task": "action"}
```

Server → client (`ActionResponse`):

```json
{"detected_classes": [{"class_name": "using computer", "conf": 0.72}]}
```

`whitelist` is optional; when set, only those Kinetics classes are scored. When
`person_detection_enabled` is true the largest person is cropped before inference.

---

## Facial emotion — WebSocket + HTTP

```
WS   /hal/api/dl/emotion-analysis/ws
GET  /hal/api/dl/emotion-labels
POST /hal/api/dl/emotion-recognize
```

**WebSocket** — client → server:

```json
{"type": "config", "task": "emotion", "threshold": 0.5, "frame_interval": 1.0}
{"type": "frame", "task": "emotion", "frame_b64": "<base64 JPEG>"}
{"type": "heartbeat", "task": "emotion"}
```

Server → client (`EmotionResponse`):

```json
{"detections": [
  {"emotion": "Happy", "confidence": 0.82, "face_confidence": 0.95,
   "bbox": [x, y, w, h], "valence": null, "arousal": null}
]}
```

`valence` / `arousal` are populated only by the EmoNet models; POSTER V2 leaves
them `null`.

**HTTP `POST /hal/api/dl/emotion-recognize`** — classify a single pre-cropped face:

```json
// request  (EmotionRecognizeRequest)
{"image_b64": "<base64 face crop>", "threshold": 0.5}
// response (EmotionRecognizeResponse) — same shape as the WS detections array
{"detections": [{"emotion": "Happy", "confidence": 0.82, "face_confidence": 1.0, "bbox": [0,0,W,H]}]}
```

**HTTP `GET /hal/api/dl/emotion-labels`** → `{"labels": ["Happy", "Sad", ...]}`
(label set of the active model).

> HAL produces face crops on-device (InsightFace) and calls the HTTP endpoint;
> the WS endpoint runs its own YuNet face detection on full frames.

---

## Speech emotion (SER) — HTTP

```
POST /hal/api/dl/ser/recognize
GET  /hal/api/dl/ser/labels
```

```json
// request  (RecognizeEmotionRequest)
{"audio_b64": "<base64 WAV, mono 16 kHz>", "return_scores": true}
// response (RecognizeEmotionResponse)
{"label": "happy", "confidence": 0.9981,
 "scores": {"angry": 0.001, "happy": 0.998, "...": 0.0}}
```

`scores` is the full per-label softmax map when `return_scores` is true (default),
else `null`. `GET /hal/api/dl/ser/labels` → `{"engine": "emotion2vec", "labels": [...]}`.

---

## Audio embedder (speaker) — HTTP

```
POST /hal/api/dl/audio-recognizer/embed
```

Returns speaker embedding vectors; speaker enrollment/matching itself lives on the
caller (HAL). Disabled by default (`AUDIO_EMBEDDER__ENABLED=false`).

```json
// request  (EmbedAudioRequest)
{"audios_b64": ["<base64 WAV>", "..."], "return_chunks": false}
// response (EmbedAudioResponse)
{"embedding": [0.01, -0.02, ...], "embedding_dim": 256, "chunk_embeddings": null}
```

`embedding` is L2-normalized. When `return_chunks` is true, `chunk_embeddings`
holds the per-window vectors before aggregation.

---

## Pose estimation — WebSocket

```
WS /hal/api/dl/pose-estimation/ws
```

Client → server:

```json
{"type": "config", "task": "pose", "frame_interval": 1.0,
 "confidence_threshold_2d": 0.3, "min_valid_keypoints": 6}
{"type": "frame", "task": "pose", "frame_b64": "<base64 JPEG>"}
{"type": "heartbeat", "task": "pose"}
```

Server → client (`PoseResponse`):

```json
{
  "pose_2d": {"graph_type": "coco", "joints": [[x, y], ...], "confs": [0.9, ...]},
  "pose_3d": {"graph_type": "h36m", "joints": [[x, y, z], ...], "confs": [0.8, ...]},
  "ergo":    {"...": "RULA scores + risk level"}
}
```

`pose_3d` (TCPFormer lift) and `ergo` (RULA ergonomics) are present only when those
stages are enabled. See [perceptions.md](perceptions.md#3-pose-estimation).

---

## Object detection — WebSocket + HTTP

Detector is selected by the `{detector_name}` path segment, one of
`yoloworld`, `owlv2` (only if enabled in config).
Mounted under `/api/dl` (not `/hal/api/dl`) for GO2 backward compatibility.

```
WS   /api/dl/object-detection/{detector_name}/ws
POST /api/dl/object-detect/{detector_name}
POST /api/dl/{detector_name}          # legacy: returns a flat array
GET  /api/dl/object-detect/models     # list detectors + ready state
```

**WebSocket** — client → server:

```json
{"type": "config", "task": "object", "frame_interval": 1.0,
 "classes": ["person", "chair"], "threshold": 0.25}
{"type": "frame", "task": "object", "frame_b64": "<base64 JPEG>"}
{"type": "heartbeat", "task": "object"}
```

Server → client (`ObjectResponse`):

```json
{"detections": [{"class_name": "chair", "xywh": [cx, cy, w, h], "confidence": 0.91}]}
```

`xywh` is `[center_x, center_y, width, height]` in pixels. `classes` is the
open-vocabulary prompt; omit to use the detector's default class list.

**HTTP `POST /api/dl/object-detect/{detector_name}`**:

```json
// request  (ObjectDetectRequest)
{"image_b64": "<base64 image>", "classes": ["person", "chair"]}
// response (ObjectDetectResponse)
{"detections": [{"class_name": "chair", "xywh": [cx, cy, w, h], "confidence": 0.91}]}
```

`POST /api/dl/{detector_name}` is the legacy form: identical body, but the response
is a **flat array** of detection items instead of the `{"detections": [...]}` wrapper.

`GET /api/dl/object-detect/models` → `{"models": [{"name": "yoloworld", "ready": true}, ...]}`.

---

## Health — HTTP

```
GET /hal/api/dl/health
```

```json
{"status": "ok",
 "models": {"action": true, "emotion": true, "ser": true, "pose": true,
            "audio_embedder": false,
            "object_detectors": {"yoloworld": false, "owlv2": false}}}
```

---

## Crypto — HTTP (lbserver)

```
GET /api/crypto/public-key
```

Served by `lbserver` (port `7999`), not `dlserver`. Returns the PEM-encoded RSA
public key as `text/plain`, or `404` if encryption is disabled. See
[crypto-and-loadbalancer.md](crypto-and-loadbalancer.md).

---

## Error responses

| Code | When |
|------|------|
| `400` | Bad/undecodable body, image, or audio; decryption auth-tag failure (lbserver) |
| `401` | Missing/invalid `X-API-Key` |
| `404` | `GET /api/crypto/public-key` when crypto disabled |
| `502` | lbserver: backend unreachable |
| `503` | Model/dependency unavailable for the requested perception |
| WS `1008` | lbserver: encryption required but key exchange missing |
| WS `1011` | lbserver: key exchange failed / backend unreachable |
</content>
