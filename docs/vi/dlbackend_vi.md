# DL Backend — Nhận dạng Hành động + Cảm xúc + Âm thanh

Dịch vụ backend tăng tốc GPU cho:
- nhận dạng hành động người theo thời gian thực qua WebSocket (X3D / UniformerV2 / VideoMAE),
- nhận dạng cảm xúc khuôn mặt qua WebSocket hoặc HTTP (POSTER V2 / EmoNet),
- nhận dạng cảm xúc giọng nói qua HTTP (emotion2vec_plus_large),
- phát hiện người tùy chọn cho tiền xử lý nhận dạng hành động (YOLO12),
- đăng ký/nhận dạng người nói qua HTTP APIs (AudioRecognizer).

LeLamp Pi truyền camera frame đến DL backend để phân tích hành động và cảm xúc, chuyển tiếp WAV cuối phát ngôn cho cảm xúc giọng nói, và client có thể đăng ký/nhận dạng người nói qua các endpoint `/api/dl/audio-recognizer/*` có xác thực.

## Kiến trúc

```
Pi (LeLamp) / Clients             Load Balancer (:7999)      DL Backend (nginx :8888 → uvicorn :8001)
┌──────────────────────┐         ┌─────────────────┐        ┌──────────────────────────────────────┐
│ Camera 640x480       │ WS/HTTP │ RSA+AES-GCM     │  HTTP  │ /api/dl/action-analysis/ws            │
│ frame_b64 every tick │────────→│ decrypt/encrypt  │───────→│ Action model (X3D/UniformerV2) ONNX  │
│                      │←────────│ round-robin proxy│←───────│ detected_classes                      │
├──────────────────────┤         └─────────────────┘        ├──────────────────────────────────────┤
│ Face crop (base64)   │  (mã hóa tùy chọn tại LB)        │ /api/dl/emotion-recognize             │
│ từ InsightFace       │────────────────────────────────────→│ Emotion model (POSTER V2/EmoNet) ONNX│
│                      │←────────────────────────────────────│ emotion + confidence                  │
├──────────────────────┤                                    ├──────────────────────────────────────┤
│ WAV cuối phát ngôn   │   HTTP                             │ /api/dl/ser/recognize                 │
│ (dùng chung speaker) │────────────────────────────────────→│ SER model (emotion2vec) ONNX         │
│                      │←────────────────────────────────────│ label + confidence (9 lớp)            │
├──────────────────────┤                                    ├──────────────────────────────────────┤
│ App / tools          │   HTTP                             │ /api/dl/audio-recognizer/*            │
│ wav URL/chunks/PCM16 │────────────────────────────────────→│ register/recognize/list/remove        │
└──────────────────────┘                                    └──────────────────────────────────────┘
```

## Models

### Nhận dạng Hành động

Chọn qua biến `ACTION_RECOGNITION_MODEL`:

| Model | Enum | File ONNX | Input | Frames mặc định |
|---|---|---|---|---|
| **X3D** (mặc định) | `x3d` | `x3d_m_16x5x1_int8.onnx` | 256×256 | 16 |
| **UniformerV2** | `uniformerv2` | Do người dùng cung cấp | 224×224 | 8 |
| **VideoMAE** | `videomae` | `videomae_int8.onnx` | 224×224 | 16 |

Tất cả phân loại từ **Kinetics-400**, lọc bởi whitelist (`white_list.txt`).

### Nhận dạng Cảm xúc

Chọn qua biến `EMOTION_RECOGNITION_MODEL`:

| Model | Enum | File ONNX | Input | Output |
|---|---|---|---|---|
| **POSTER V2** (mặc định) | `posterv2` | `posterv2_7cls.onnx` | 224×224, ImageNet norm | 7 cảm xúc (RAF-DB) |
| **EmoNet-8** | `emonet_8` | `emonet_8.onnx` | 256×256 | 8 cảm xúc + valence + arousal |
| **EmoNet-5** | `emonet_5` | `emonet_5.onnx` | 256×256 | 5 cảm xúc + valence + arousal |

Phát hiện khuôn mặt cho cảm xúc dùng **YuNet** (`face_detection_yunet_2023mar.onnx`). Tách biệt với InsightFace của LeLamp (dùng cho nhận dạng danh tính trên thiết bị).

### Nhận dạng Cảm xúc Giọng nói (SER)

Chọn qua biến `SER_RECOGNITION_MODEL`:

| Model | Enum | File ONNX | Input | Output |
|---|---|---|---|---|
| **emotion2vec_plus_large** (mặc định) | `emotion2vec_plus_large` | xuất từ FunASR snapshot khi cold start | mono 16 kHz waveform | 9 lớp + softmax confidence |

### Phát hiện Người (Tùy chọn)

Khi bật, YOLO12 phát hiện người lớn nhất trong mỗi frame và cắt trước khi đưa vào model nhận dạng hành động.

| Thiết lập | Mặc định |
|---|---|
| Model | `yolo12x.pt` (Ultralytics) |
| Bật | `false` |
| Ngưỡng confidence | 0.4 |
| Bbox expand scale | 2.0 |

### Nhận dạng Người nói

Chọn qua biến `AUDIO_RECOGNIZER_ENGINE`:

| Model | Enum | File ONNX | Embedding dim |
|---|---|---|---|
| **WeSpeaker ResNet34** (mặc định) | `resnet34` | `voxceleb_resnet34_LM.onnx` | 256 |
| ECAPA-TDNN 1024 | `ecapa-tdnn1024` | `voxceleb_ECAPA1024_LM.onnx` | — |
| CAM++ | `campplus` | `voxceleb_CAM++.onnx` | — |

## API Endpoints

### Phân tích Hành động (WebSocket)

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

### Nhận dạng Cảm xúc (HTTP)

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

### Nhận dạng Cảm xúc Giọng nói (HTTP)

```
POST /api/dl/ser/recognize
GET  /api/dl/ser/labels
```

**Request (base64):**
```json
{"audio_b64": "<base64 WAV (mono 16 kHz)>", "return_scores": false}
```

**Response:**
```json
{"label": "happy", "confidence": 0.9981, "scores": null}
```

### Nhận dạng Âm thanh (HTTP)

Đường dẫn gốc: `/api/dl/audio-recognizer`

| Method | Path | Mô tả |
|---|---|---|
| POST | `/register` | Đăng ký người nói |
| POST | `/recognize` | Nhận dạng người nói |
| GET | `/speakers` | Liệt kê người nói đã đăng ký |
| DELETE | `/speakers/{name}` | Xóa người nói |

### Health Check

```
GET /api/dl/health
→ {"status": "ok", "action_model": true, "emotion_model": true}
```

## Luồng Dữ liệu

### Phân tích Hành động

1. **Pi**: `SensingService._tick()` đọc camera frame mỗi 2s
2. **Pi**: `MotionPerception` → `RemoteMotionChecker.update()` mã hóa frame thành base64 JPEG
3. **WebSocket**: `{"type": "frame", "frame_b64": "..."}` gửi đến RunPod với header `X-API-Key`
4. **RunPod**: Model hành động đệm frames, chạy inference mỗi `frame_interval`
5. **RunPod**: Nếu bật person detector → cắt người lớn nhất trước, rồi phân loại
6. **RunPod**: Tiền xử lý (BGR→RGB, center crop, normalization), chạy softmax trên whitelist
7. **WebSocket**: Trả về `{"detected_classes": [["walking", 0.87], ["reading book", 0.42]]}`
8. **Pi**: Đệm actions + snapshots trong `MOTION_FLUSH_S`, rồi gửi event tổng hợp
9. **Pi → Lumi**: `POST /api/sensing/event` với `type: "motion.activity"`

### Phân tích Cảm xúc

1. **Pi**: `SensingService._tick()` phát hiện khuôn mặt qua InsightFace (trên thiết bị)
2. **Pi**: `EmotionPerception` cắt khuôn mặt, mã hóa base64 JPEG
3. **HTTP**: `POST /api/dl/emotion-recognize` với face crop + threshold
4. **RunPod**: YuNet phát hiện lại khuôn mặt (tùy chọn), POSTER V2 / EmoNet phân loại cảm xúc
5. **HTTP**: Trả về `{"detections": [{"emotion": "Happy", "confidence": 0.82}]}`
6. **Pi**: Đệm, áp dụng polarity-bucket dedup, phát `emotion.detected` event

## Cấu hình

### RunPod (.env)

```env
DL_API_KEY=<shared secret>

# Nhận dạng hành động: x3d | uniformerv2 | videomae
ACTION_RECOGNITION_MODEL=x3d

# Nhận dạng cảm xúc: posterv2 | emonet_8 | emonet_5
EMOTION_RECOGNITION_MODEL=posterv2

# Nhận dạng người nói: resnet34 | ecapa-tdnn1024 | campplus
AUDIO_RECOGNIZER_ENGINE=resnet34
```

### Pi (.env)

```env
DL_BACKEND_URL=wss://<POD_ID>-8888.proxy.runpod.net/lelamp/api/dl/action-analysis/ws
DL_API_KEY=<shared secret>
LELAMP_MOTION_ENABLED=true
```

### Ngưỡng (lelamp/config.py)

| Tham số | Mặc định | Mục đích |
|---|---|---|
| `MOTION_CONFIDENCE_THRESHOLD` | 0.3 | Ngưỡng confidence tối thiểu cho hành động |
| `MOTION_FLUSH_S` | 10.0 | Khoảng thời gian flush bộ đệm (giây) |
| `EMOTION_CONFIDENCE_THRESHOLD` | cấu hình được | Ngưỡng confidence tối thiểu cho cảm xúc |
| `SPEECH_EMOTION_ENABLED` | `true` | Bật/tắt nhận dạng cảm xúc giọng nói |
| `DL_ENCRYPTION_ENABLED` | `false` | Bật mã hóa phía client cho DL backend |
| `DL_ENCRYPTION_REQUIRED` | `false` | Thất bại nếu thiết lập mã hóa lỗi (không fallback plaintext) |

## Xác thực

- Các route HTTP dưới `/api/dl/*` dùng header `X-API-Key` khi `DL_API_KEY` được đặt.
- Các endpoint WebSocket xác thực `X-API-Key` từ WS headers khi kết nối.
- Nếu `DL_API_KEY` rỗng, xác thực bị tắt (chế độ dev).

## Mã hóa (RSA + AES-256-GCM)

Mã hóa hybrid tùy chọn được xử lý tại tầng **load balancer**. DL server (dlserver) vẫn dùng plaintext — LB giải mã request đến và mã hóa response đi một cách trong suốt.

### Kiến trúc

```
LeLamp (client)                          Load Balancer (:7999)                    DL Server (:8001)
┌──────────────┐   traffic mã hóa      ┌──────────────────────┐   plaintext     ┌──────────────┐
│ CryptoSession │ ────────────────────→ │ RSAAESCrypto         │ ─────────────→  │ FastAPI       │
│ (AES-256-GCM) │ ←──────────────────── │ giải mã → chuyển tiếp│ ←─────────────  │ (không crypto)│
└──────────────┘                        │ mã hóa ← response   │                 └──────────────┘
                                        └──────────────────────┘
```

### Thuật toán Mã hóa

| Thành phần | Thuật toán | Chi tiết |
|---|---|---|
| Trao đổi khóa | RSA-OAEP (SHA-256) | Client mã hóa AES session key 32 byte ngẫu nhiên bằng RSA public key của LB |
| Mã hóa dữ liệu | AES-256-GCM | Nonce 12 byte, có xác thực (tag nhúng trong cipher_data) |
| Cặp khóa | RSA 2048-bit (cấu hình được) | Tạo khi khởi động; lưu đĩa nếu `CRYPTO__KEY_DIR` được đặt |

### Endpoint Public Key

```
GET /api/dl/public-key
→ RSA public key dạng PEM (text/plain)
→ 404 nếu crypto bị tắt
```

LeLamp lấy key này khi khởi động để mã hóa session keys. Hoặc có thể cung cấp PEM qua biến `DL_PUBLIC_KEY_PEM` (bỏ qua việc fetch).

### Mã hóa HTTP

Khi crypto bật và body request khớp schema `CipherHTTPRequest`, LB giải mã trước khi chuyển tiếp và mã hóa response.

**Client → LB (request đã mã hóa):**
```json
{
  "encrypted_key": "<base64 RSA-OAEP(AES session key)>",
  "nonce": "<base64 nonce GCM 12 byte>",
  "cipher_data": "<base64 AES-GCM(plaintext + tag)>"
}
```

**LB → Client (response đã mã hóa):**
```json
{
  "nonce": "<base64 nonce GCM 12 byte>",
  "cipher_data": "<base64 AES-GCM(plaintext + tag)>"
}
```

Request plaintext (không mã hóa) vẫn đi qua khi `CRYPTO__REQUIRE_ENCRYPTION=false`.

### Mã hóa WebSocket

Sau khi kết nối, client thực hiện trao đổi khóa trước khi gửi frames:

**1. Trao đổi khóa (client → LB):**
```json
{"type": "key_exchange", "encrypted_key": "<base64 RSA-OAEP(AES session key)>"}
```

**2. Phản hồi trao đổi khóa (LB → client):**
```json
{"status": "key_exchange_ok"}
```

**3. Tất cả message tiếp theo dùng `WSCipherMessage`:**
```json
{"type": "encrypted", "nonce": "<base64>", "cipher_data": "<base64>"}
```

Nếu bỏ qua trao đổi khóa và `CRYPTO__REQUIRE_ENCRYPTION=false`, message đi qua dạng plaintext. Nếu `CRYPTO__REQUIRE_ENCRYPTION=true`, LB đóng kết nối với code 1008.

### Cấu hình

#### Load Balancer (dlbackend/.env)

| Biến | Mặc định | Mô tả |
|---|---|---|
| `CRYPTO__ENABLED` | `true` | Bật mã hóa tại LB |
| `CRYPTO__KEY_DIR` | `None` | Thư mục lưu RSA keys (bỏ qua = in-memory) |
| `CRYPTO__KEY_SIZE` | `2048` | Kích thước RSA key (bits) |
| `CRYPTO__REQUIRE_ENCRYPTION` | `false` | Từ chối request plaintext |

#### LeLamp Client (lelamp/.env)

| Biến | Mặc định | Mô tả |
|---|---|---|
| `LELAMP_DL_ENCRYPTION` | `false` | Bật mã hóa phía client |
| `LELAMP_DL_ENCRYPTION_REQUIRED` | `false` | Thất bại nếu thiết lập mã hóa lỗi (không fallback plaintext) |
| `DL_PUBLIC_KEY_PEM` | _(rỗng)_ | RSA public key PEM (nếu đặt, bỏ qua fetch từ LB) |

## Triển khai

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

## Các loại Event

| Event | Khi nào | Gửi cho Agent |
|---|---|---|
| `motion.activity` | Có người + phát hiện hành động | Có, kèm danh sách hành động |
| `motion` | Chuyển động lớn, không nhận ra người | Có, "có thể có người vào" |
| `emotion.detected` | Cảm xúc khuôn mặt trên ngưỡng confidence | Có, kèm nhãn cảm xúc + confidence |

## Xử lý Lỗi

- **WebSocket ngắt kết nối**: `RemoteMotionChecker` bắt `ConnectionClosedError`, kết nối lại ở tick tiếp theo
- **RunPod không khả dụng**: `DL_BACKEND_URL` rỗng → perception không được tạo, bỏ qua
- **Model chưa tải**: Server trả WebSocket close "Model not ready" / HTTP 503
- **Frame lỗi**: Server log warning, bỏ qua frame, tiếp tục
- **Audio không có file**: `422` với thông báo validation
- **Audio URL không phải http/https**: lỗi validation (đường dẫn local bị từ chối)

## File Quan trọng

### dlbackend/

| File | Mục đích |
|---|---|
| `src/server.py` | FastAPI app, tải model, WS + HTTP routes, health check |
| `src/config.py` | Pydantic settings: chọn model, cấu hình per-model, person detector |
| `src/core/crypto/rsa_aes.py` | Mã hóa hybrid RSA+AES-256-GCM (`RSAAESCrypto`, `AESGCMSession`) |
| `src/core/models/crypto.py` | Dataclass payload cho crypto (`AESGCMCipherPayload`, `RSAAESCipherPayload`, v.v.) |
| `src/lbserver/app.py` | Load balancer — round-robin HTTP/WS proxy với mã hóa |
| `src/lbserver/models.py` | Pydantic wire-format models (`CipherHTTPRequest`, `WSCipherMessage`, v.v.) |
| `src/lbserver/utils/crypto.py` | Helper giải mã/mã hóa HTTP cho LB proxy |
| `nginx.conf` | Reverse proxy :8888 → :8001, strip prefix `/lelamp/`, WS upgrade |

### lelamp/ (phía Pi)

| File | Mục đích |
|---|---|
| `service/sensing/perceptions/processors/motion.py` | `RemoteMotionChecker` — WS client, mã hóa frame, đệm hành động |
| `service/sensing/perceptions/processors/emotion.py` | `RemoteEmotionRecognizer` — HTTP client, face crop → phân loại cảm xúc |
| `service/sensing/crypto.py` | `CryptoSession` phía client, wire-format models, phân giải public key |
| `service/voice/speech_emotion/service.py` | `SpeechEmotionService` — queue + worker + flush + dedup + Lumi POST |
| `config.py` | `DL_BACKEND_URL`, `DL_API_KEY`, ngưỡng, `SPEECH_EMOTION_*` |
| `service/sensing/sensing_service.py` | Điều phối tất cả perceptions trong `_tick()` |

## Nginx Routing

```
:8888 (public)
├── /              → 127.0.0.1:8000
└── /lelamp/       → 127.0.0.1:8001  (strip prefix /lelamp/, bật WS upgrade)
```

Tất cả traffic LeLamp đi qua `/lelamp/` → port 8001 (FastAPI). Routes có prefix `/api/dl/` trên FastAPI.
