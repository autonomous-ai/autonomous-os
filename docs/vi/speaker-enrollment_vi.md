# Đăng ký giọng nói (Speaker Enrollment) — Tài liệu kỹ thuật

**Trạng thái: ĐÃ TRIỂN KHAI** (2026-04)

## Tổng quan

Lamp nhận diện người nói qua **WeSpeaker ResNet34** (vector nhúng 256 chiều, ONNX Runtime). Khi không nhận ra người nói, LeLamp lưu audio và tuỳ điều kiện sẽ yêu cầu AI agent đăng ký giọng nói. Đăng ký chỉ áp dụng **tự phục vụ** — mỗi người tự đăng ký giọng nói của mình.

## Kiến trúc

```
┌─────────────────────────────────────────────────────────────────────┐
│  LeLamp (Python, port 5001)                                         │
│                                                                     │
│  VoiceService._stream_session()                                     │
│    ├─ STT chuyển giọng nói → văn bản                                │
│    ├─ _identify_and_decorate(transcript)                            │
│    │   ├─ audio_buffer → WAV bytes → base64                        │
│    │   ├─ POST /audio-recognizer/embed → dlbackend (RunPod)        │
│    │   │   └─ WeSpeaker ResNet34 ONNX → vector 256 chiều           │
│    │   ├─ Bình chọn theo từng chunk so với embedding đã đăng ký     │
│    │   ├─ Khớp ≥ 0.7 → "Speaker - Tên: transcript"                 │
│    │   └─ Không khớp → _format_unknown_speaker()                   │
│    │       ├─ _should_request_enroll() kiểm tra điều kiện           │
│    │       │   ├─ ≥ 25 từ trong transcript                          │
│    │       │   └─ ≥ 5 giây audio                                    │
│    │       ├─ ĐẠT → "Unknown Speaker: ... (audio save at <path>,   │
│    │       │          auto enroll ...)"                              │
│    │       └─ KHÔNG ĐẠT → "Unknown Speaker: ..." (không kèm yêu   │
│    │          cầu đăng ký)                                          │
│    └─ POST /api/sensing/event → Lamp (Go)                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Lamp (Go, port 5000)                                               │
│                                                                     │
│  Hai đường đi (cả hai gọi domain.AppendEnrollNudge):                │
│                                                                     │
│  1. Đường trực tiếp (handler.go)                                    │
│     └─ Agent rảnh → gửi thẳng tới OpenClaw                         │
│                                                                     │
│  2. Đường hàng đợi (service.go)                                     │
│     └─ Agent bận → xếp hàng → phát lại khi agent rảnh              │
│                                                                     │
│  AppendEnrollNudge(msg) — domain/voice.go:                          │
│    ├─ Kiểm tra: chứa "Unknown Speaker:" + "audio save at"          │
│    ├─ Cooldown: bỏ qua nếu < 5 phút kể từ lần nhắc trước          │
│    └─ Chèn: "[REQUIRED: Follow speaker-recognizer/SKILL.md ...]"   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  OpenClaw Agent                                                     │
│                                                                     │
│  speaker-recognizer/SKILL.md                                        │
│    ├─ Phát hiện tự giới thiệu ("I'm X", "tôi là X", "mình là X")  │
│    ├─ curl POST /speaker/enroll với wav_path + tên                  │
│    ├─ Hai lượt: hỏi "Bạn là ai?" → đăng ký với cả hai path        │
│    └─ Xác nhận: "Rất vui được biết bạn, Tên!"                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Chống spam — Bốn lớp bảo vệ

Bốn lớp ngăn agent hỏi "bạn là ai?" liên tục:

| Lớp | Vị trí | Điều kiện | Mục đích |
|-----|--------|-----------|----------|
| **Thời lượng audio** | LeLamp `voice_service.py` | `duration_s < SPEAKER_MIN_AUDIO_S` (0.8s) | Bỏ qua nhận diện hoàn toàn cho audio quá ngắn |
| **Yêu cầu đăng ký** | LeLamp `_should_request_enroll()` | `≥ 15 từ VÀ ≥ 2s audio` | Không kèm instruction đăng ký đầy đủ cho câu ngắn (biến thể ngắn kèm gợi ý combine vẫn được gửi) |
| **Cooldown nhắc nhở phía Lamp** | Lamp `domain/voice.go` | `5 phút kể từ lần nhắc trước` | Không chèn SKILL.md instruction quá 1 lần mỗi 5 phút |
| **Cooldown theo voiceprint** | LeLamp `voice_service.py` | `30 phút mỗi voiceprint_hash` (`LELAMP_ENROLL_NUDGE_COOLDOWN_S`) | Không lặp lại "hỏi tên user" cho cùng một cluster giọng lạ; gửi message `Unknown Speaker:` trần |

## Model & Embedding

| Thuộc tính | Giá trị |
|------------|---------|
| Model | WeSpeaker ResNet34 (huấn luyện trên VoxCeleb) |
| Chiều embedding | 256 |
| Runtime | ONNX Runtime (CPU) trên dlbackend (RunPod) |
| Endpoint | `POST {DL_BACKEND_URL}/lelamp/api/dl/audio-recognizer/embed` |
| Xác thực | Header `X-API-Key` |
| Timeout | 15 giây |

### Thuật toán nhận diện

1. Audio → tiền xử lý (giảm nhiễu, VAD, lọc cao tần, chuẩn hoá RMS)
2. Trích xuất embedding theo từng chunk `[M, 256]`
3. Cosine similarity với tất cả embedding người nói đã đăng ký
4. Bình chọn theo chunk: mỗi chunk vote cho người khớp nhất
5. Người thắng = nhiều vote nhất (hoà thì so trung bình confidence)
6. `confidence ≥ 0.7` → khớp; ngược lại → không xác định

### Chất lượng đăng ký

1. Mỗi file WAV → embedding qua dlbackend
2. Lọc theo ngưỡng consistency `0.7` (cosine similarity giữa các mẫu)
3. Tổng hợp embedding còn lại qua trung bình có trọng số
4. Lưu vector chuẩn hoá L2 tại `/root/local/users/{tên}/voice/embedding.npy`

### Theo dõi cụm giọng lạ (`voiceprint_hash`)

Mọi giọng lạ được gom cụm local để server biết "đây là cùng một người đã nghe cách đây 3 phút" mà không cần backend hỗ trợ. Cho phép agent gộp nhiều câu ngắn thành 1 lần enroll.

1. Sau khi embedding audio, recognizer tổng hợp embedding theo chunk thành 1 vector chuẩn hoá L2.
2. So với các centroid cụm stranger đã lưu (cosine similarity).
3. Match ≥ `LELAMP_VOICE_STRANGER_MATCH_THRESHOLD` (mặc định `0.65`, thấp hơn 0.7 của known-speaker để cùng giọng gom chung thay vì phân mảnh) → dùng lại label `voice_N`.
4. Không match → tạo label mới `voice_{counter}`, thêm centroid vào state trên đĩa.
5. Giới hạn `LELAMP_MAX_VOICE_STRANGERS` (mặc định `50`) — evict oldest khi vượt.
6. Hash được:
   - trả trong response recognize dưới field `voiceprint_hash: "voice_N"` (null cho known speaker)
   - gắn vào message nudge dạng tag `[voice:voice_N]` để skill đối chiếu qua các turn
   - dùng để group WAV vào subdir (xem Lưu trữ)

**Trim silence cuối**: trước khi WAV đi qua embedding API, buffer speaker-ID được cắt tại frame speech cuối cùng + 200ms tail. Nếu không, câu 3s sẽ thành ~5.5s với ~45% silence, làm loãng embedding. Chỉ ảnh hưởng path speaker-ID — STT vẫn nhận đủ stream.

## Cấu hình

| Tham số | Mặc định | Biến môi trường | Mô tả |
|---------|----------|-----------------|-------|
| Ngưỡng khớp | 0.7 | `SPEAKER_MATCH_THRESHOLD` | Confidence tối thiểu để khớp |
| Ngưỡng consistency khi đăng ký | 0.7 | `SPEAKER_ENROLL_CONSISTENCY_THRESHOLD` | Cosine similarity tối thiểu giữa các mẫu |
| Timeout API | 15s | `SPEAKER_EMBEDDING_API_TIMEOUT_S` | Timeout HTTP cho embedding API |
| Audio tối thiểu cho nhận diện | 0.8s | `LELAMP_SPEAKER_MIN_AUDIO_S` | Bỏ qua nhận diện dưới ngưỡng này |
| Số từ tối thiểu cho nudge đăng ký | 15 | Hardcoded trong `_should_request_enroll()` | Cổng số từ transcript |
| Thời lượng tối thiểu cho nudge đăng ký | 2.0s | Hardcoded trong `_should_request_enroll()` | Cổng thời lượng audio |
| Cooldown nhắc nhở phía Lamp | 5 phút | Hardcoded trong `domain/voice.go` | Không inject SKILL instruction toàn cục quá 1 lần/5 phút |
| Cooldown nhắc nhở theo voiceprint | 30 phút | `LELAMP_ENROLL_NUDGE_COOLDOWN_S` | Không hỏi lại tên cho cùng cluster voiceprint |
| Ngưỡng match voice stranger | 0.65 | `LELAMP_VOICE_STRANGER_MATCH_THRESHOLD` | Cosine similarity để gom giọng lạ vào `voice_N` đã có |
| Số voice stranger tối đa | 50 | `LELAMP_MAX_VOICE_STRANGERS` | Giới hạn cluster; evict oldest khi vượt |
| Thư mục voice strangers | `/root/local/voice_strangers` | `LELAMP_VOICE_STRANGERS_DIR` | Persist embedding cluster (tồn tại qua reboot) |
| Bật/tắt nhận diện giọng nói | false | `LELAMP_SPEAKER_RECOGNITION_ENABLED` | Công tắc tổng |

## Lưu trữ

```
/root/local/users/{tên}/
  metadata.json                      # Danh tính chung (telegram, display_name)
  voice/
    embedding.npy                    # Vector chuẩn hoá L2 [256]
    metadata.json                    # num_samples, dim, timestamps
    sample_{origin}_{ts}_{uuid}.wav  # Các mẫu đăng ký (16kHz mono)

/tmp/lamp-unknown-voice/
  incoming_{ts}_{uuid}.wav           # Audio known-speaker (phẳng)
  voice_{N}/
    incoming_{ts}_{uuid}.wav         # Audio unknown — gom theo cụm voiceprint

/root/local/voice_strangers/
  embeds.npy                         # Centroid các cluster stranger [N, 256]
  labels.npy                         # Label cluster ["voice_1", "voice_2", ...]
  counter.npy                        # Counter tăng cho label mới
```

## API Endpoints (LeLamp, port 5001)

| Method | Path | Mô tả |
|--------|------|-------|
| `POST` | `/speaker/enroll` | Đăng ký giọng nói từ wav_paths + tên |
| `POST` | `/speaker/recognize` | Nhận diện người nói từ wav_path |
| `POST` | `/speaker/identity` | Liên kết Telegram với profile giọng nói |
| `POST` | `/speaker/remove` | Xoá profile giọng nói theo tên |
| `POST` | `/speaker/reset` | Xoá tất cả profile giọng nói |
| `GET`  | `/speaker/list` | Liệt kê người nói đã đăng ký |

### Hợp đồng lỗi (error contract)

`/speaker/enroll` phân biệt hai loại thất bại:

| HTTP | Khi nào | Hành vi skill |
|------|---------|---------------|
| `400` | Audio bị reject (quá ngắn, im lặng, VAD không tìm thấy speech, dlbackend trả 4xx) | Yêu cầu user thu lại / nói rõ hơn |
| `503` | Embedding service không reachable (network, 5xx, response malformed) | Báo user thử lại sau — disk không bị thay đổi gì |

`/speaker/recognize` **không bao giờ** trả 5xx khi embedding API chết — nó trả `200` với `{name: "unknown", error: "<lý do>"}` để skill tự xử graceful. Chỉ lỗi input (thiếu WAV, base64 sai) mới trả `400`.

## Vị trí code chính

| Thành phần | File | Hàm/Struct |
|------------|------|------------|
| STT → nhận diện người nói | `lelamp/service/voice/voice_service.py` | `_identify_and_decorate()` |
| Cổng đăng ký | `lelamp/service/voice/voice_service.py` | `_should_request_enroll()` |
| Định dạng message | `lelamp/service/voice/voice_service.py` | `_format_unknown_speaker()` |
| Bộ nhận diện giọng nói | `lelamp/service/voice/speaker_recognizer/speaker_recognizer.py` | `SpeakerRecognizer` |
| Chèn instruction + cooldown | `lamp/domain/voice.go` | `AppendEnrollNudge()` |
| Đường trực tiếp | `lamp/server/sensing/delivery/http/handler.go` | `PostEvent()` |
| Đường hàng đợi/phát lại | `lamp/internal/openclaw/service.go` | `drainPendingEvents()` |
| Skill agent | `lamp/resources/openclaw-skills/speaker-recognizer/SKILL.md` | — |
| Model embedding | `dlbackend/src/core/audio_recognition/audio_recognizer.py` | `ResNet34Recognizer` (mặc định), `EcapaTdnn1024Recognizer`, `CamPPlusRecognizer` — chọn qua env `AUDIO_RECOGNIZER_ENGINE` |
| Endpoint embedding | `dlbackend/src/protocols/htpp/audio_recognizer.py` | `embed_audio()` |
| Cấu hình | `lelamp/config.py` | Các hằng số `SPEAKER_*` |

## Ví dụ luồng message

### Câu ngắn (bị chặn)
```
User nói: "hey" (2 từ, 0.9s audio)
→ LeLamp: bỏ qua nhận diện (< SPEAKER_MIN_AUDIO_S)
→ Message: "hey" (không prefix, không instruction đăng ký)
```

### Câu trung bình (nhận diện nhưng không nudge đăng ký)
```
User nói: "bật đèn lên đi" (4 từ, 3s audio)
→ LeLamp: nhận diện → unknown, _should_request_enroll(4 từ, 3s) = false
→ Message: "Unknown Speaker: bật đèn lên đi"
→ Lamp: không có "audio save at" → AppendEnrollNudge giữ nguyên
→ Agent: phản hồi bình thường, không hỏi user là ai
```

### Gộp nhiều turn ngắn (cùng cluster giọng)
```
Turn 1: "nice to meet you today. Okay." (5 từ)
→ LeLamp: recognize → unknown, voiceprint_hash=voice_5
→ WAV chuyển vào /tmp/lamp-unknown-voice/voice_5/incoming_A.wav
→ Message: "Unknown Speaker: [voice:voice_5] nice to meet you today. Okay. (audio saved at ..._A.wav. Note: audio is too short for single enrollment. If prior turns tagged the same voice_5, combine their saved paths...)"
→ Agent: hỏi "Cho mình biết tên bạn với?"

Turn 2: "I'm Alex." (2 từ)
→ LeLamp: voiceprint_hash=voice_5 (cùng cluster, sim=0.75)
→ WAV chuyển vào /tmp/lamp-unknown-voice/voice_5/incoming_B.wav
→ Message: "Unknown Speaker: [voice:voice_5] I'm Alex. (audio saved at ..._B.wav...)"
→ Agent: quét các turn trước cùng tag [voice:voice_5] → tìm thấy path A
→ Agent: POST /speaker/enroll với wav_paths=[path_A, path_B], name="Alex"
→ Agent: "Rất vui được biết bạn, Alex!"
```

### Câu dài (luồng đăng ký đầy đủ)
```
User nói: "Xin chào mình là Leo, mình vừa đi làm về..." (30 từ, 8s audio)
→ LeLamp: nhận diện → unknown, _should_request_enroll(30 từ, 8s) = true
→ Message: "Unknown Speaker: Xin chào mình là Leo... (audio save at /tmp/lamp-unknown-voice/incoming_xxx.wav, auto enroll...)"
→ Lamp: AppendEnrollNudge → cooldown OK → chèn "[REQUIRED: Follow speaker-recognizer/SKILL.md...]"
→ Agent: phát hiện "mình là Leo" → POST /speaker/enroll → "Rất vui được biết bạn, Leo!"
```

### Cooldown (bị chặn)
```
Cùng unknown speaker, 2 phút sau:
→ LeLamp: _should_request_enroll = true (đủ dài)
→ Message có "audio save at"
→ Lamp: AppendEnrollNudge → cooldown CHƯA hết (< 5 phút) → bỏ qua instruction
→ Agent: thấy "Unknown Speaker: ..." không có SKILL instruction → phản hồi bình thường
```
