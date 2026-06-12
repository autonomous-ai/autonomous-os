# Nhận Diện Cảm Xúc Giọng Nói (SER)

HAL phân tích cảm xúc từ giọng nói **sau mỗi phiên mic** (VAD trigger → im lặng ~2.5 s đóng phiên), độc lập với việc STT có trả transcript hay không. Nhờ vậy, tiếng cười, thở dài, "ờ ờ" và các tín hiệu phi-lời nói (vốn để lại transcript rỗng) vẫn được phân loại. Kết quả được gom theo người dùng, lọc trùng theo bucket cảm xúc, rồi gửi sự kiện `speech_emotion.detected` tới OS server để OpenClaw phản ứng. Speaker recognition vẫn được gọi nội bộ để xác định trường `user` (rơi về `unknown` khi không nhận diện được); nó **không còn là cổng chặn** trước SER.

**Tài liệu liên quan:** [Tuning sensing (SER)](../sensing-tuning.md#speech-emotion-recognition-ser) · [dlbackend](../dlbackend.md) · [Sensing behavior](sensing-behavior_vi.md)

---

## Kiến Trúc

```
VoiceService._stream_session(...) finally   ← cuối MỌI phiên mic
    │
    ├─ Trim đuôi im lặng trên audio_buffer
    │
    ├─ Wake-word split trên `combined` → final_text + event_type
    │       (chỉ chạy khi STT có transcript)
    │
    ├─ _identify_and_decorate(final_text, audio_buffer)   ← 1 LẦN duy nhất / phiên
    │       → (final_msg, user_name | None)
    │  user = user_name hoặc "unknown"
    │
    ├─ if combined:
    │       _send_to_lamp(final_msg, event_type)   ← POST OS server voice / voice_command
    │
    └─ _submit_speech_emotion_from_session(audio_buffer, user)   ← LUÔN
            │
            ├─ _session_wav_for_ser(audio_buffer) → (wav_bytes, duration_s) | None
            │
            └─ SpeechEmotionService.submit(user, wav_bytes, duration_s)
                    │
                    ├─ Worker: POST dlbackend /api/dl/ser/recognize
                    │       → label + confidence → buffer _Inference theo user
                    │
                    └─ Flush mỗi SPEECH_EMOTION_FLUSH_S:
                            mode label → bucket → dedup → POST OS server speech_emotion.detected
```

**Tách bạch SER khỏi STT, dùng chung speaker recognize:**
- `_identify_and_decorate` chạy **đúng 1 lần** mỗi phiên — kết quả phục vụ cả OS server message lẫn SER user.
- OS server POST chỉ chạy khi STT có transcript; SER submit chạy mọi phiên (kể cả tiếng cười, sighs).
- `_submit_speech_emotion_from_session` giờ nhận `user` qua tham số, **không tự gọi speaker** nữa.
- Closure `_send_best` cũ đã được inline trực tiếp vào finally block.

---

## Module `speech_emotion/`

| File | Vai trò |
|------|---------|
| `service.py` | `SpeechEmotionService`: queue, worker HTTP, flush, dedup |
| `recognizer.py` | `Emotion2VecRecognizer`: POST WAV tới dlbackend |
| `labels.py` | Map label model → bucket OS server (`positive` / `negative` / `neutral`) |
| `messages.py` | Chuỗi human-readable cho event message |

### `SpeechEmotionService`

- **Khởi tạo:** `VoiceService` tạo instance khi `SPEECH_EMOTION_ENABLED` và dlbackend URL sẵn sàng.
- **`submit(user, wav_bytes, duration_s)`** — trả về ngay (non-blocking). Bỏ qua nếu: service tắt, `user`/`wav` rỗng, `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` (mặc định **3.0s**), queue đầy.
- **Worker:** gọi API; bỏ mẫu có `confidence < CONFIDENCE_THRESHOLD_BY_LABEL[label]` (per-label, khai báo trong `constants.py` — xem mục Cấu Hình).
- **Flush:** mỗi `SPEECH_EMOTION_FLUSH_S` giây, gom buffer theo `user`, lấy **mode** label, map bucket, bỏ **neutral**, dedup `(user, bucket)` trong `SPEECH_EMOTION_DEDUP_WINDOW_S`, POST OS server.

### `_Job` vs `_Inference`

| Struct | Thời điểm | Nội dung |
|--------|-----------|----------|
| `_Job` | Trước API | `user`, `wav_bytes`, `duration_s` — item trong queue worker |
| `_Inference` | Sau API | `label`, `confidence`, `duration_s`, `ts` — append vào buffer flush theo `user` |

---

## Tích Hợp `voice_service.py`

| Hàm | Vai trò |
|-----|---------|
| `_identify_and_decorate` | Speaker `/embed` + prefix transcript (`Alice: ...` / `Unknown Speaker: ...`). Trả `(final_msg, user_name)`: `user_name` = tên khi match; `UNKNOWN_USER_LABEL` (`"unknown"`) khi API OK nhưng không match; `None` khi skip/lỗi/tắt speaker. **Không gọi SER.** |
| `_session_wav_for_ser` | Mono 16 kHz WAV + `duration_s` từ `audio_buffer` (cần `>= SPEAKER_MIN_AUDIO_S`, mặc định 0.8s). |
| `_submit_speech_emotion_from_session` | Orchestrator mới: build WAV → gọi `_identify_and_decorate("", buffer)` lấy `user_name` → fallback `"unknown"` → `SpeechEmotionService.submit(...)`. Được gọi **bất điều kiện** trong `_stream_session` finally. |
| `_stream_session` finally | Inline toàn bộ: wake-word split → 1 lần `_identify_and_decorate(final_text, buffer)` → POST OS server `voice` / `voice_command` (nếu có transcript) → `_submit_speech_emotion_from_session(buffer, user)`. Closure `_send_best` cũ đã được gỡ. |

### Gán `user` cho SER

| Tình huống speaker | `user_name` từ identify | `user` gửi SER |
|--------------------|-------------------------|----------------|
| Match tên | `"alice"` | `"alice"` |
| Không match (API OK) | `"unknown"` | `"unknown"` |
| Lỗi / exception / speaker tắt / buffer ngắn | `None` | `"unknown"` (fallback trong `_submit_speech_emotion_from_session`) |

Transcript gửi OS server vẫn có thể là `Unknown Speaker:` trong khi SER dùng key dedup chung `unknown` cho mọi người lạ.

---

## Khi Nào **Không** Gọi SER

| Điều kiện | Ghi chú |
|-----------|---------|
| `SPEECH_EMOTION_ENABLED = False` | Hoặc dlbackend không cấu hình |
| Buffer STT quá ngắn | `_session_wav_for_ser` trả `None` (< `SPEAKER_MIN_AUDIO_S`) |
| `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` | `submit()` bỏ qua (mặc định 3.0s) |
| Queue đầy | Log warning, bỏ job |
| Confidence thấp | Worker không buffer |
| Label neutral sau flush | Không POST OS server |
| Dedup `(user, bucket)` | Trong cửa sổ `SPEECH_EMOTION_DEDUP_WINDOW_S` |

**VAD:** chỉ mở phiên mic phía trước (`_vad_loop`); không có VAD thứ hai trước SER. Cuối phiên (im lặng 2.5s) đóng mic → SER tự kích hoạt từ finally block, **không cần STT có transcript**.

**Speaker fail vs unknown:** Lỗi speaker chỉ ảnh hưởng `user` field; SER vẫn enqueue với `"unknown"` nếu audio đủ dài (`>= SPEAKER_MIN_AUDIO_S` cho build WAV và `>= SPEECH_EMOTION_MIN_AUDIO_S` cho `submit()`).

**Transcript rỗng (laughter, cough, sigh):** Trước đây bị chặn ở cổng `if combined`; hiện tại vẫn vào SER. Nếu mô hình `emotion2vec` map laughter sang `happy`/`surprised` và confidence ≥ ngưỡng, event sẽ được gửi đi.

---

## Sự Kiện OS Server

```
POST http://127.0.0.1:5000/api/sensing/event
{
  "type": "speech_emotion.detected",
  "message": "Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...)",
  "current_user": "alice",
  "audio": "/tmp/hal-speech-emotion/1715587812413_alice_sad.wav"
}
```

Trường `audio` là field **riêng, tùy chọn** — đường dẫn trên đĩa tới clip WAV sinh ra event này. Nó **không** nằm trong `message` và **không bao giờ** được gửi tới LLM (xem [Lưu Audio Để Gỡ Lỗi](#lưu-audio-để-gỡ-lỗi) bên dưới). Rỗng khi persistence bị tắt hoặc ghi file thất bại.

OpenClaw / sensing pipeline xử lý như sự kiện sensing khác (xem [sensing-behavior_vi.md](sensing-behavior_vi.md)).

---

## Lưu Audio Để Gỡ Lỗi

Để debug các lần đọc SER nhiễu, service lưu lại clip WAV đứng sau mỗi event và hiển thị nó trong Flow Monitor UI dưới dạng trình phát bấm-để-nghe. **Đây chỉ là công cụ gỡ lỗi — audio không bao giờ được gửi tới LLM.**

### Phía ghi (HAL)

Trong `_process_job`, mọi inference vượt qua ngưỡng confidence theo label đều được ghi ra đĩa bởi `_persist_wav()` trước khi vào buffer:

- **Thư mục:** `SPEECH_EMOTION_AUDIO_DIR` (cấu hình trong `os/hal/config.py`, env `HAL_SPEECH_EMOTION_AUDIO_DIR`), mặc định `<tempdir>/hal-speech-emotion` (tức `/tmp/hal-speech-emotion`). Tạo bằng `os.makedirs(exist_ok=True)` lúc khởi tạo; nếu tạo thất bại thì thư mục bị tắt và mọi POST mang trường `audio` rỗng (graceful degradation — SER vẫn chạy bình thường).
- **Tên file:** `<ms>_<user>_<label>.wav`, trong đó `<ms>` là timestamp inference tính bằng mili-giây, còn `<user>`/`<label>` được sanitize về `[a-zA-Z0-9_-]` (ký tự khác gộp thành `_`).
- **Chọn lúc flush:** khi flush của một user phát ra label dominant non-neutral, nó đính kèm clip **mới nhất** trong nhóm inference cùng label dominant — `max(dom_inferences, key=lambda i: i.ts).audio_path` — làm trường `audio` trong POST.

### Phía phục vụ (OS server)

OS server backend chỉ expose clip cho Flow Monitor UI qua route mới `GET /api/sensing/audio/:name` (`SensingHandler.GetAudio`). Nó phục vụ WAV theo **basename** (đường dẫn đầy đủ không bao giờ rời khỏi thiết bị) từ một trong:

```
/var/lib/hal/speech-emotion
/tmp/hal-speech-emotion
```

Basename được kiểm tra (đuôi `.wav`, không chứa `/`, `\`, hay `..`) trước khi phục vụ. Trong `PostEvent`, đường dẫn `audio` thô được map sang URL phục vụ được (`/api/sensing/audio/<name>`) và đính vào detail của event Monitor `sensing_input`; turn item của Monitor render thành trình phát audio bấm được. Đường dẫn thô không bao giờ lộ ra UI, và trường `audio` không bao giờ bị nối vào chuỗi chat gửi đi.

### Giới hạn đã biết: không tự dọn dẹp

Mọi inference đạt ngưỡng đều được lưu WAV — kể cả clip neutral / non-dominant không bao giờ trở thành event. Hiện **chưa có cơ chế tự động dọn** thư mục audio, nên nó có thể phình lên theo thời gian và cần được dọn thủ công (hoặc qua housekeeping bên ngoài) trên thiết bị chạy lâu.

---

## Cấu Hình (`os/hal/config.py`)

| Hằng số | Mặc định | Ý nghĩa |
|---------|----------|---------|
| `SPEECH_EMOTION_ENABLED` | `True` | Bật module |
| `SPEECH_EMOTION_FLUSH_S` | `10.0` | Chu kỳ flush buffer / user |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | `300.0` | TTL dedup `(user, bucket)` |
| `SPEECH_EMOTION_MIN_AUDIO_S` | `3.0` | Độ dài tối thiểu utterance |
| `SPEECH_EMOTION_API_TIMEOUT_S` | `15` | Timeout HTTP dlbackend |
| `DL_SER_ENDPOINT` | `/lelamp/api/dl/ser/recognize` | Path SER |

**Ngưỡng confidence theo từng label** không lấy từ env nữa — khai báo cố định trong `os/hal/service/voice/speech_emotion/constants.py`:

```python
CONFIDENCE_THRESHOLD_BY_LABEL: dict[str, float] = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5  # fallback cho label không nằm trong dict
```

Negative emotion siết chặt hơn để tránh false positive gây alarm. Worker lookup qua `utils.threshold_for(label)`; muốn tune → sửa trực tiếp `constants.py`, không có env override.

Chi tiết tuning / log: [sensing-tuning_vi.md](sensing-tuning_vi.md).

---

## Quan Hệ Với Các Hệ Thống Khác

| Hệ thống | Quan hệ |
|----------|---------|
| Speaker recognition | Cùng WAV session; decorate transcript tách với SER |
| STT (Deepgram) | SER chạy sau khi phiên mic kết thúc, **độc lập với transcript** — gọi từ `_stream_session` finally sau khi OS server POST (nếu có) |
| dlbackend | ONNX emotion2vec; xem [dlbackend.md](../dlbackend.md) |
| Face / motion emotion | Khác pipeline (camera); không dùng chung buffer SER |
| OS server dedup / cooldown | `speech_emotion.detected` có cooldown riêng trên OS server (nếu cấu hình) |

---

## Gỡ Lỗi

Log tag `[speech_emotion]`:

```
INFO ... [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO ... [speech_emotion] flushing alice: Speech emotion detected: Sad. ...
INFO ... [speech_emotion] sent to OS server: ...
INFO ... [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

| Triệu chứng | Hướng xử lý |
|-------------|-------------|
| Không có event | Kiểm tra enabled, độ dài audio ≥ 3s, confidence, label neutral |
| Quá nhiều event `unknown` | Kỳ vọng với người lạ; tăng threshold / dedup — không tắt SER chỉ vì transcript `Unknown Speaker:` |
| Queue full | Độ trễ dlbackend; xem timeout và tải Pi |
