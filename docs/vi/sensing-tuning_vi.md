# Tuning Sensing — Phần SER (Nhận Diện Cảm Xúc Giọng Nói)

> Tài liệu tuning đầy đủ (motion, face, sound, …) bằng tiếng Anh: [sensing-tuning.md](../sensing-tuning.md).  
> Kiến trúc SER: [speech-emotion_vi.md](speech-emotion_vi.md).

---

## Speech Emotion Recognition (SER)

**File:** `lelamp/config.py`, `lelamp/service/voice/voice_service.py` (`_submit_speech_emotion_from_session`, `_identify_and_decorate`, `_session_wav_for_ser`)

**Tích hợp voice (cuối phiên mic, độc lập transcript):** trong `finally` của `_stream_session`, `_identify_and_decorate(final_text, audio_buffer)` chạy **đúng 1 lần** để lấy đồng thời `final_msg` (cho Lumi POST khi STT có chữ) và `user_name` (cho SER submit). Sau đó gọi `_submit_speech_emotion_from_session(audio_buffer, user=...)` — chỉ build WAV và `SpeechEmotionService.submit`, không gọi speaker lần 2. Người không match / lỗi speaker vẫn enqueue SER dưới key dedup chung `unknown` nếu audio đủ dài.

```python
SPEECH_EMOTION_ENABLED = True
SPEECH_EMOTION_FLUSH_S = 10.0               # Chu kỳ drain buffer theo user
SPEECH_EMOTION_DEDUP_WINDOW_S = 300.0       # TTL (user, bucket) — 5 phút
SPEECH_EMOTION_MIN_AUDIO_S = 3.0            # Bỏ utterance ngắn hơn (mặc định config)
SPEECH_EMOTION_API_TIMEOUT_S = 15           # Timeout HTTP dlbackend
DL_SER_ENDPOINT = "/lelamp/api/dl/ser/recognize"
```

Ngưỡng confidence **per-label** không nằm trong `config.py` — khai báo trong `lelamp/service/voice/speech_emotion/constants.py` qua `CONFIDENCE_THRESHOLD_BY_LABEL` (và `DEFAULT_CONFIDENCE_THRESHOLD` cho label không map). Negative emotion siết chặt hơn positive để giảm false positive:

```python
# constants.py
CONFIDENCE_THRESHOLD_BY_LABEL = {
    "happy":     0.5,
    "surprised": 0.6,
    "sad":       0.6,
    "angry":     0.6,
    "fearful":   0.7,
    "disgusted": 0.7,
}
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
```

Sửa trực tiếp dict để tune — không còn env override.

### Đọc log

Service gắn tag `[speech_emotion]`:

```
INFO lelamp.voice.speech_emotion: [speech_emotion] buffered: alice -> sad (0.72, 2.40s)
INFO lelamp.voice.speech_emotion: [speech_emotion] flushing alice: Speech emotion detected: Sad. (weak voice cue; confidence=0.72; bucket=negative; ...) (mode of sad, fearful, sad)
INFO lelamp.voice.speech_emotion: [speech_emotion] sent to Lumi: Speech emotion detected: Sad. ...
INFO lelamp.voice.speech_emotion: [speech_emotion] dedup drop: angry bucket=negative (key seen 87.4s ago)
```

Dòng `flushing` hiển thị danh sách label thô — đó là mode trên các mẫu trong buffer.

### Tuning

| Triệu chứng | Cách chỉnh |
|-------------|------------|
| Cùng bucket fire quá thường xuyên | Tăng `SPEECH_EMOTION_DEDUP_WINDOW_S` (300 → 600) |
| Một utterance nhiễu vẫn lọt | Tăng entry tương ứng trong `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) — ví dụ `"sad": 0.6 → 0.7`. Chỉ tăng `DEFAULT_CONFIDENCE_THRESHOLD` khi nhiễu diện rộng |
| "Ừ" / "ok" ngắn bị flag | Tăng `SPEECH_EMOTION_MIN_AUDIO_S` (3.0 → 4.0) |
| Lumi phản ứng chậm sau đổi mood thật | Giảm `SPEECH_EMOTION_FLUSH_S` (10 → 5) |
| Cảnh báo worker queue full | Kiểm tra độ trễ dlbackend; tăng queue không đủ nếu downstream kẹt |
| Quá nhiều `speech_emotion.detected` cho người lạ | **Kỳ vọng:** `user="unknown"`; siết entry per-label trong `CONFIDENCE_THRESHOLD_BY_LABEL` (`constants.py`) hoặc dedup — **không** tắt SER chỉ vì transcript có `Unknown Speaker:` |

### Áp dụng thay đổi

Sau khi sửa `lelamp/config.py` hoặc `voice_service.py` trên Pi: restart service LeLamp (xem [lamp-server_vi.md](lamp-server_vi.md)).
