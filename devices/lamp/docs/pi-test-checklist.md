# Pi Hardware Test Checklist

Track which features have been manually tested on the Raspberry Pi 4.

**Legend:** ✅ Tested & working | ❌ Tested, broken | ⏳ Not tested yet | ⚠️ Partial

---

## Infrastructure — Test trước, mọi thứ đều phụ thuộc vào đây

| # | Component | How to test | Status | Notes |
|---|---|---|---|--|
| INF-01 | HAL startup | SSH vào Pi, chạy `sudo systemctl status lelamp` hoặc `python server.py` trực tiếp. Expect: không có exception, log "Application startup complete" | ✅ | |
| INF-02 | Lamp startup | `sudo systemctl status os-server` hoặc chạy binary trực tiếp. Expect: log "connected to OpenClaw WebSocket" | ✅ | |
| INF-03 | LED driver | `curl -X POST http://pi:5001/led/solid -d '{"r":255,"g":100,"b":0,"brightness":80}'` → LED sáng màu cam | ✅ | |
| INF-04 | Servo driver | `curl -X POST http://pi:5001/servo/move -d '{"positions":{"tilt":90}}'` → servo tilt di chuyển | ✅ | |
| INF-05 | Audio playback | `curl -X POST http://pi:5001/voice/speak -d '{"text":"hello","language":"en"}'` → nghe thấy giọng nói qua speaker | ✅ | |
| INF-06 | Mic capture | `curl -X POST http://pi:5001/voice/start` → nói thử → `curl http://pi:5001/voice/status` xem có transcript không | ✅ | |
| INF-07 | Camera | `curl http://pi:5001/camera` → `{"available":true}`. Rồi `curl http://pi:5001/camera/snapshot -o test.jpg` → mở file xem ảnh có rõ không | ✅ | |
| INF-08 | Sensing loop | Đứng trước camera → xem log Lamp có nhận `POST /api/sensing/event` với `type:"presence.enter"` không | ✅ | |
| INF-09 | OpenClaw WS | Xem log Lamp khi start. Expect: `[openclaw] websocket connected`. Gửi thử 1 message từ Telegram/Web UI → có response không | ✅ | |

---

## P0 — MVP Core

| # | Use Case | How to test | Status | Notes |
|---|---|---|---|---|
| UC-01 | Voice control | Nói: **"bật đèn"** → LED bật. Nói: **"tắt đèn"** → LED tắt. Nói: **"sáng hơn"** → LED tăng brightness | ✅ | |
| UC-02 | LED color via voice | Nói: **"đèn màu xanh"** → LED xanh. Nói: **"đèn vàng ấm"** → LED vàng. Nói: **"màu hoàng hôn"** → LED gradient cam-hồng | ✅ | |
| UC-14 | Voice reply (TTS + body language) | Hỏi: **"hôm nay thời tiết thế nào?"** → Lamp trả lời bằng giọng + servo cử động + LED đổi theo cảm xúc khi nói | ✅ | |

---

## P1 — Launch-critical

| # | Use Case | How to test | Status | Notes |
|---|---|---|---|---|
| UC-03 | Scene presets | Nói: **"chế độ làm việc"** → LED trắng sáng. Nói: **"thư giãn"** → LED vàng ấm tối. Nói: **"xem phim"** → LED dim amber. Nói: **"đi ngủ"** → LED tắt dần | ✅ | |
| UC-04 | Scheduling | Nói: **"30 giây nữa tắt đèn"** → đợi 30s → LED tắt. Nói: **"hủy timer"** → timer bị cancel | ✅ | |
| UC-06 | AI assistant | Nói: **"dịch hello sang tiếng Việt"** → trả lời đúng. Nói: **"thời tiết Hà Nội hôm nay"** → có thông tin thời tiết | ✅ | |
| UC-08 | Servo via voice | Nói: **"nghiêng sang trái"** → servo tilt trái. Nói: **"hướng xuống bàn"** → servo cúi xuống. Nói: **"thẳng lên"** → servo về thẳng | ✅ | |
| UC-11 | Presence detection | **Enter:** Rời xa rồi bước vào khung hình camera → Lamp tự chào (không cần nói gì). **Leave:** Rời khỏi tầm nhìn camera > 15 phút → đèn tự dim/tắt. **Noise check:** Ngồi yên gõ phím bình thường → Lamp không bị trigger bởi micro-movement (motion threshold tuning) | ⚠️ | `presence_service.py` + SOUL.md greet rule có đủ, chưa test thực tế trên Pi |
| UC-13 | System status LED | **Boot:** Tắt/bật Pi → quan sát LED sequence (booting → connecting → ready). **Listening:** Nói wake word → LED đổi màu báo hiệu đang nghe | ✅ | Listening state (cyan breathing) implemented + tested |

---

## Extra — Guard Mode & Stranger Tracking

| # | Use Case | How to test | Status | Notes |
|---|---|---|---|---|
| EX-01 | Guard mode enable/disable | `curl -X POST http://pi:5000/api/guard/enable` → `{"status":1}`. `curl http://pi:5000/api/guard` → `{"guard_mode":true}`. `curl -X POST http://pi:5000/api/guard/disable` → `{"guard_mode":false}` | ✅ | API + config + skill done |
| EX-02 | Guard mode broadcast | Bật guard mode → bước vào khung hình camera → kiểm tra tất cả Telegram DM/group nhận được cảnh báo presence.enter kèm ảnh | ✅ | SSE handler broadcasts via TelegramSender, snapshot attached |
| EX-03 | Guard mode motion broadcast | Bật guard mode → tạo chuyển động lớn trước camera → kiểm tra Telegram nhận cảnh báo motion | ✅ | Same broadcast path as EX-02 |
| EX-04 | Guard manual alert | `curl -X POST http://pi:5000/api/guard/alert -d '{"message":"Test alert"}'` → tất cả chat session nhận message | ✅ | `PostGuardAlert` → `Broadcast()` |
| EX-05 | Guard mode via voice | Nói: **"bật chế độ canh gác"** hoặc **"enable guard mode"** → guard mode bật. Nói: **"tắt canh gác"** → guard mode tắt | ✅ | OpenClaw `guard` skill with enable/disable API + camera auto-enable |
| EX-06 | Stranger stats tracking | Để stranger xuất hiện trước camera nhiều lần → `curl http://pi:5001/face/stranger-stats` → thấy count tăng, first_seen/last_seen đúng | ✅ | HAL `facerecognizer.py` tracks visit counts per stranger ID |
| EX-07 | Stranger enrollment suggestion | Để stranger xuất hiện 3+ lần → agent gợi ý đăng ký khuôn mặt | ⚠️ | Sensing skill has context but no explicit visit-count trigger yet |
| EX-08 | Stranger stats persistence | Restart HAL → `curl http://pi:5001/face/stranger-stats` → stats vẫn còn (lưu trong HAL data dir) | ✅ | Persisted to `.stranger_stats.json` |

---

## Extra — Speaker Recognition & Voice Enrollment

| # | Use Case | How to test | Status | Notes |
|---|---|---|---|---|
| EX-09 | Speaker recognition | Nói gì đó → xem transcript có prefix `Name:` (recognized) hoặc `Unknown:` (chưa enroll) | ✅ | HAL `speaker_recognizer.py` + Lamp `speaker-recognizer` skill |
| EX-10 | Voice self-enrollment | Nói **"I'm Leo"** hoặc **"tôi là Leo"** khi chưa enroll → agent tự enroll voice profile từ audio path | ✅ | Skill triggers on `Unknown Speaker:... (audio save at <path>)` + self-intro |
| EX-11 | Telegram voice enrollment | Gửi voice note trên Telegram kèm giới thiệu tên → agent enroll voice + link Telegram identity | ✅ | Skill handles `[mediaPaths: ...]` + intro detection |

---

## Extra — Facial Emotion & Wellness (AI-Driven)

| # | Use Case | How to test | Status | Notes |
|---|---|---|---|---|
| EX-12 | Facial emotion detection | Ngồi trước camera, thể hiện cảm xúc → xem log có `emotion.detected` event | ✅ | HAL `emotion.py` via dlbackend WS, 7 emotions (Angry/Disgust/Fear/Happy/Sad/Surprise/Neutral) |
| EX-13 | Mood logging from emotion | `emotion.detected` → agent tự POST `/api/mood/log` → `curl http://pi:5000/api/openclaw/mood-history` có entry | ✅ | `user-emotion-detection` skill → `mood` skill pipeline |
| EX-14 | Proactive wellness nudge | Ngồi làm việc lâu (sedentary activity detected) → Lamp nhắc uống nước / đứng dậy | ✅ | `wellbeing` skill, event-driven from `motion.activity` sedentary labels |
| EX-15 | Proactive music suggestion | Mood decision logged (stressed/tired/etc.) → Lamp gợi ý nhạc phù hợp | ✅ | `music-suggestion` skill, triggers on mood decisions + sedentary activity |

---

## Known gaps (not testing — P2+)

- UC-09: Face tracking / servo follow face — chưa implement
- UC-10: Gesture control — chưa implement
- UC-12: Video call lighting — chưa implement
- UC-M4a: Screen-time / eye-care tracking (gaze estimation) — chưa implement
- UC-M4b: Wellness gestures (MediaPipe) — chưa implement
