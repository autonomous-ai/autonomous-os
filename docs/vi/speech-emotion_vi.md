# Nhận Diện Cảm Xúc Giọng Nói (SER)

LeLamp phân tích cảm xúc từ giọng nói **sau mỗi phiên mic** (VAD trigger → im lặng ~2.5 s đóng phiên), độc lập với việc STT có trả transcript hay không. Nhờ vậy, tiếng cười, thở dài, "ờ ờ" và các tín hiệu phi-lời nói (vốn để lại transcript rỗng) vẫn được phân loại. Kết quả được gom theo người dùng, lọc trùng theo bucket cảm xúc, rồi gửi sự kiện `speech_emotion.detected` tới Lumi để OpenClaw phản ứng. Speaker recognition vẫn được gọi nội bộ để xác định trường `user` (rơi về `unknown` khi không nhận diện được); nó **không còn là cổng chặn** trước SER.

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
    │       _send_to_lumi(final_msg, event_type)   ← POST Lumi voice / voice_command
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
                            mode label → bucket → dedup → POST Lumi speech_emotion.detected
```

**Tách bạch SER khỏi STT, dùng chung speaker recognize:**
- `_identify_and_decorate` chạy **đúng 1 lần** mỗi phiên — kết quả phục vụ cả Lumi message lẫn SER user.
- Lumi POST chỉ chạy khi STT có transcript; SER submit chạy mọi phiên (kể cả tiếng cười, sighs).
- `_submit_speech_emotion_from_session` giờ nhận `user` qua tham số, **không tự gọi speaker** nữa.
- Closure `_send_best` cũ đã được inline trực tiếp vào finally block.

---

## Module `speech_emotion/`

| File | Vai trò |
|------|---------|
| `service.py` | `SpeechEmotionService`: queue, worker HTTP, flush, dedup |
| `recognizer.py` | `Emotion2VecRecognizer`: POST WAV tới dlbackend |
| `labels.py` | Map label model → bucket Lumi (`positive` / `negative` / `neutral`) |
| `messages.py` | Chuỗi human-readable cho event message |

### `SpeechEmotionService`

- **Khởi tạo:** `VoiceService` tạo instance khi `SPEECH_EMOTION_ENABLED` và dlbackend URL sẵn sàng.
- **`submit(user, wav_bytes, duration_s)`** — trả về ngay (non-blocking). Bỏ qua nếu: service tắt, `user`/`wav` rỗng, `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` (mặc định **3.0s**), queue đầy.
- **Worker:** gọi API; bỏ mẫu có `confidence < SPEECH_EMOTION_CONFIDENCE_THRESHOLD`.
- **Flush:** mỗi `SPEECH_EMOTION_FLUSH_S` giây, gom buffer theo `user`, lấy **mode** label, map bucket, bỏ **neutral**, dedup `(user, bucket)` trong `SPEECH_EMOTION_DEDUP_WINDOW_S`, POST Lumi.

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
| `_stream_session` finally | Inline toàn bộ: wake-word split → 1 lần `_identify_and_decorate(final_text, buffer)` → POST Lumi `voice` / `voice_command` (nếu có transcript) → `_submit_speech_emotion_from_session(buffer, user)`. Closure `_send_best` cũ đã được gỡ. |

### Gán `user` cho SER

| Tình huống speaker | `user_name` từ identify | `user` gửi SER |
|--------------------|-------------------------|----------------|
| Match tên | `"alice"` | `"alice"` |
| Không match (API OK) | `"unknown"` | `"unknown"` |
| Lỗi / exception / speaker tắt / buffer ngắn | `None` | `"unknown"` (fallback trong `_submit_speech_emotion_from_session`) |

Transcript Lumi vẫn có thể là `Unknown Speaker:` trong khi SER dùng key dedup chung `unknown` cho mọi người lạ.

---

## Khi Nào **Không** Gọi SER

| Điều kiện | Ghi chú |
|-----------|---------|
| `SPEECH_EMOTION_ENABLED = False` | Hoặc dlbackend không cấu hình |
| Buffer STT quá ngắn | `_session_wav_for_ser` trả `None` (< `SPEAKER_MIN_AUDIO_S`) |
| `duration_s < SPEECH_EMOTION_MIN_AUDIO_S` | `submit()` bỏ qua (mặc định 3.0s) |
| Queue đầy | Log warning, bỏ job |
| Confidence thấp | Worker không buffer |
| Label neutral sau flush | Không POST Lumi |
| Dedup `(user, bucket)` | Trong cửa sổ `SPEECH_EMOTION_DEDUP_WINDOW_S` |

**VAD:** chỉ mở phiên mic phía trước (`_vad_loop`); không có VAD thứ hai trước SER. Cuối phiên (im lặng 2.5s) đóng mic → SER tự kích hoạt từ finally block, **không cần STT có transcript**.

**Speaker fail vs unknown:** Lỗi speaker chỉ ảnh hưởng `user` field; SER vẫn enqueue với `"unknown"` nếu audio đủ dài (`>= SPEAKER_MIN_AUDIO_S` cho build WAV và `>= SPEECH_EMOTION_MIN_AUDIO_S` cho `submit()`).

**Transcript rỗng (laughter, cough, sigh):** Trước đây bị chặn ở cổng `if combined`; hiện tại vẫn vào SER. Nếu mô hình `emotion2vec` map laughter sang `happy`/`surprised` và confidence ≥ ngưỡng, event sẽ được gửi đi.

---

## Sự Kiện Lumi

```
POST http://127.0.0.1:5000/api/sensing/event
{
  "type": "speech_emotion.detected",
  "message": "Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...)",
  "metadata": { "user": "alice", "label": "sad", "bucket": "negative", "confidence": 0.72, ... }
}
```

OpenClaw / sensing pipeline xử lý như sự kiện sensing khác (xem [sensing-behavior_vi.md](sensing-behavior_vi.md)).

---

## Cấu Hình (`lelamp/config.py`)

| Hằng số | Mặc định | Ý nghĩa |
|---------|----------|---------|
| `SPEECH_EMOTION_ENABLED` | `True` | Bật module |
| `SPEECH_EMOTION_CONFIDENCE_THRESHOLD` | `0.5` | Ngưỡng tối thiểu sau API |
| `SPEECH_EMOTION_FLUSH_S` | `10.0` | Chu kỳ flush buffer / user |
| `SPEECH_EMOTION_DEDUP_WINDOW_S` | `300.0` | TTL dedup `(user, bucket)` |
| `SPEECH_EMOTION_MIN_AUDIO_S` | `3.0` | Độ dài tối thiểu utterance |
| `SPEECH_EMOTION_API_TIMEOUT_S` | `15` | Timeout HTTP dlbackend |
| `DL_SER_ENDPOINT` | `/lelamp/api/dl/ser/recognize` | Path SER |

Chi tiết tuning / log: [sensing-tuning_vi.md](sensing-tuning_vi.md).

---

## Quan Hệ Với Các Hệ Thống Khác

| Hệ thống | Quan hệ |
|----------|---------|
| Speaker recognition | Cùng WAV session; decorate transcript tách với SER |
| STT (Deepgram) | SER chạy sau khi phiên mic kết thúc, **độc lập với transcript** — gọi từ `_stream_session` finally sau khi Lumi POST (nếu có) |
| dlbackend | ONNX emotion2vec; xem [dlbackend.md](../dlbackend.md) |
| Face / motion emotion | Khác pipeline (camera); không dùng chung buffer SER |
| Lumi dedup / cooldown | `speech_emotion.detected` có cooldown riêng trên Lumi (nếu cấu hình) |

---

## Gỡ Lỗi

Log tag `[speech_emotion]`:

```
INFO ... [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO ... [speech_emotion] flushing alice: Speech emotion detected: Sad. ...
INFO ... [speech_emotion] sent to Lumi: ...
INFO ... [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

| Triệu chứng | Hướng xử lý |
|-------------|-------------|
| Không có event | Kiểm tra enabled, độ dài audio ≥ 3s, confidence, label neutral |
| Quá nhiều event `unknown` | Kỳ vọng với người lạ; tăng threshold / dedup — không tắt SER chỉ vì transcript `Unknown Speaker:` |
| Queue full | Độ trễ dlbackend; xem timeout và tải Pi |
