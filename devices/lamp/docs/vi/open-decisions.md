# Quyết Định — AI Lamp (Lamp)

> Tất cả blocking decisions đã chốt. Document này track lại toàn bộ quyết định.

## Chưa chốt

| Quyết định | Bối cảnh | Phương án | Đề xuất |
|---|---|---|---|
| Channel abstraction layer | UC-15 multi-channel (Telegram/Slack/Discord) hiện "miễn phí" nhờ OpenClaw built-in. Nhưng nếu đổi gateway khác (không phải OpenClaw), multi-channel sẽ mất. | (1) Không làm gì — chấp nhận lock-in OpenClaw cho channels. (2) Build channel abstraction ở Lamp level để messaging hoạt động bất kể gateway. (3) Defer cho đến khi thực sự cần đổi gateway. | Option 3 (YAGNI), nhưng thiết kế UC-15 với ý thức rằng channel support phụ thuộc gateway. Ghi rõ dependency. |

---

## Đã chốt (2026-03-24)

| Quyết định | Kết quả | Docs |
|---|---|---|
| Bridge Go ↔ Python | HTTP proxy. LeLamp FastAPI `127.0.0.1:5001`, Lamp proxy từ port 5000. | `architecture-decision.md` §11, `bootstrap-ota.md` §6 |
| LeLamp source | Mono-repo. Copy drivers từ `humancomputerlab/lelamp_runtime` vào `lelamp/`. Track upstream qua `UPSTREAM.md`. | `bootstrap-ota.md` §6 |
| Tên project/character | **Lamp** (from "luminous"). Binary: `lamp-server`. Service: `lamp.service`. Wake word: "Hey Lamp". | Tất cả docs |
| Display concept | Dual-mode: pixel-art eyes (default) + info display (giờ, thời tiết, timer, notifications). | `architecture-decision.md` §3, `product-vision.md` §4 |
| Autonomous sensing | Hybrid. Lamp chạy edge detection nhẹ. Đẩy event cho OpenClaw khi cần AI quyết định. | `product-vision.md` §2 Pillar 4 |
| OTA components | 5 thành phần: lamp, bootstrap, web, openclaw, lelamp. | `bootstrap-ota.md` §1-§3 |
| Product pillars | 4 Trụ cột: "Hiểu tôi", "Sống thật", "Hữu ích thật", "Tự hành". | `product-vision.md` §2 |

## Đã chốt (2026-03-25)

| Quyết định | Kết quả |
|---|---|
| Loại bỏ GWS | Xóa toàn bộ GWS handlers, scripts, domain types. MQTT chỉ còn: `info`, `add_channel`, `ota`. |
| Inline LLM service | Xóa `internal/llm/`. `ListModelsFromAPI` inline vào `openclaw/service.go`. |
| Loại bỏ onboarding | Xóa `onboarding.go`. Setup flow đơn giản hóa. |
| Dọn dẹp scripts | Xóa `release-*.sh`, GWS scripts. Thêm `upload-lelamp.sh`. |
| Đổi tên thư mục | Toàn bộ code vào `lamp/`. |
| LED driver ownership | LeLamp Python rpi_ws281x own toàn bộ LED. Go SPI driver đã xóa. |
| SKILL.md (#1) | 9 skills: led-control, servo-control, camera, audio, emotion, sensing, scene, display, scheduling. Tất cả mô tả HTTP API tại `127.0.0.1:5001`. |
| Event push (#2) | WebSocket RPC `chat.send` với `operator.write` scope. LeLamp POST → Lamp Go `/api/sensing/event` → OpenClaw WS. |
| Camera processing (#3) | On-device OpenCV trong LeLamp Python. Frame diff cho motion, Haar cascade cho face detection, mean brightness cho light level. Auto-snapshot (full-resolution JPEG q85) khi event đáng kể → forward OpenClaw vision. |
| AI Vision | Bật (`SupportsVision: true`, `Input: ["text", "image"]`). Sensing event có ảnh gửi qua `SendChatMessageWithImage` → AI nhìn được camera snapshot. |
| Face detection vs recognition | **Cả hai done.** Face detection (P1) qua Haar cascade. Face **recognition** (P2) qua InsightFace embeddings — `facerecognizer.py` phân loại owner vs stranger, fire `presence.enter` kèm tên hoặc `stranger_N`. Enrollment qua `/face/enroll` API + `face-enroll/SKILL.md`. Stranger visit tracking có persistence. |
| Voice/speaker identification | **Done (2026-04).** LeLamp `speaker_recognizer.py` nhận diện người nói bằng voice embedding. Transcript có prefix `Name:` (đã enroll) hoặc `Unknown:` (chưa enroll). Tự enroll qua voice intro hoặc Telegram voice note. |
| Enrolled gating strategy | **Done (2026-04).** Dual-gate: face recognition (InsightFace) + voice recognition (`speaker_recognizer.py`). Cả hai chạy on-device. Người lạ phân loại `Unknown`/`stranger`. Per-user data gated bằng identity. Self-enrollment cho cả face (`/face/enroll`) và voice (qua skill). |
| Audio/Voice (#4) | LeLamp own mic/speaker. Local VAD (RMS energy) + on-demand Deepgram STT. Wake word "Hey Lamp" trong transcript → `voice_command` (ưu tiên). Không có wake word → `voice` (ambient sensing). |
| Emotion presets (#6) | 8 presets (curious, happy, sad, thinking, idle, excited, shy, shock) + 11 eye expressions trên display. |
| Display rendering (#7) | `gc9a01-python` + PIL/Pillow. 240x240 round LCD. Dual-mode eyes/info. Auto-blink. Plugin — skip nếu không có. |
| Lighting scenes | 6 presets: reading, focus, relax, movie, night, energize. Simulated color temp qua RGB mixing. |
| Presence auto-control | State machine PRESENT → IDLE (5 phút) → AWAY (15 phút). Motion quay lại → restore light. |
| Scheduling | OpenClaw built-in cron (default on). Chỉ cần SKILL.md, không cần code thêm. |
| AGENTS.md | Dùng default của OpenClaw. Custom rules tune sau khi test trên Pi. |

---

## Implementation Status

### P0 — First Prototype ✅ (code xong, cần test Pi)

| UC | Feature | File chính |
|---|---------|-----------|
| UC-01 | Voice control lighting | `voice_service.py`, `led-control/SKILL.md` |
| UC-02 | Color & color temp | `server.py /led/*`, `scene/SKILL.md` |
| UC-14 | Audio feedback | `tts_service.py`, `audio/SKILL.md` |

### P1 — v1.0 ✅ (code xong)

| UC | Feature | File chính |
|---|---------|-----------|
| UC-03 | Scene/mood presets | `server.py SCENE_PRESETS`, `scene/SKILL.md` |
| UC-04 | Timer & schedule | OpenClaw cron, `scheduling/SKILL.md` |
| UC-06 | AI companion | OpenClaw + `SOUL.md` + `emotion/SKILL.md` |
| UC-08 | Servo direction | `server.py /servo/*`, `servo-control/SKILL.md` |
| UC-11 | Presence detection (face detection + presence.enter/leave + light level + auto-snapshot vision) | `sensing_service.py`, `presence_service.py`, `sensing/SKILL.md` |
| UC-13 | Status indication | 🟡 Partial — boot/error có, processing/timer chưa |

### P2 — v1.x (chưa code, không blocking)

| UC | Feature | Ghi chú |
|---|---------|---------|
| UC-05 | Circadian lighting | Cần scheduler + color temp curve |
| UC-07 | Light effects (breathing, rainbow) | ✅ Partial — breathing LED + color drift trong `internal/ambient/`. Rainbow/candle chưa có. |
| UC-09 | Auto-tracking (camera → servo) | Face detection → servo loop |
| UC-10 | Gesture control | Hand pose estimation |
| UC-12 | Video call optimization | Face lighting analysis |
| UC-15 | Remote control (Telegram/Slack) | OpenClaw multi-channel — **Lưu ý:** hiện "free" nhờ OpenClaw. Nếu đổi gateway khác, cần channel abstraction layer. Xem mục Chưa chốt. |
| — | Face recognition ✅ | InsightFace embeddings, owner/stranger, `/face/enroll` + `face-enroll/SKILL.md` |
| — | Voice/speaker recognition ✅ | LeLamp `speaker_recognizer.py`, voice embedding, self-enrollment qua voice intro hoặc Telegram voice note |
| — | Facial emotion detection ✅ | dlbackend WS emotion classifier, `emotion.detected`, `user-emotion-detection/SKILL.md` |
| — | Proactive wellness ✅ | `wellbeing/SKILL.md`, event-driven từ `motion.activity` sedentary labels |
| — | Proactive music suggestion ✅ | `music-suggestion/SKILL.md`, mood + sedentary triggers |

### 4 Pillars ✅

| Pillar | Status | Code |
|--------|--------|------|
| 1. "Hiểu tôi" | ✅ | OpenClaw + SOUL.md + long-term memory |
| 2. "Sống thật" | ✅ | Servo + LED + emotion + display eyes (11 expressions) |
| 3. "Hữu ích thật" | ✅ | Scenes, scheduling, voice assistant |
| 4. "Tự hành" | ✅ | Sensing loop + presence auto on/off + ambient idle (breathing LED, color drift, servo micro-movements, TTS self-talk) |

### Skills (18 total) ✅

| Skill | SKILL.md | Endpoints / Mô tả |
|-------|----------|-----------|
| led-control | ✅ | `/led/solid`, `/led/paint`, `/led/off` |
| servo-control | ✅ | `/servo`, `/servo/play` |
| camera | ✅ | `/camera`, `/camera/snapshot`, `/camera/stream` |
| audio | ✅ | `/audio`, `/audio/volume`, `/audio/play-tone`, `/audio/record` |
| emotion | ✅ | `/emotion` (servo + LED + eyes coordinated) |
| sensing | ✅ | Auto — motion/sound/presence/light events → OpenClaw (vision) + presence auto |
| sensing-track | ✅ | Activity/presence timeline tracking qua wellbeing JSONL |
| scene | ✅ | `/scene` (6 presets) |
| display | ✅ | `/display/eyes`, `/display/info`, `/display/snapshot` |
| voice | ✅ | Voice pipeline control, TTS routing |
| music | ✅ | Reactive music playback (`/audio/play`, `/audio/stop`) |
| music-suggestion | ✅ | Proactive music suggestion (mood + sedentary triggers) |
| mood | ✅ | Mood signal fusion + decision logging (`/api/mood/log`) |
| wellbeing | ✅ | Event-driven hydration/break reminders (`/api/openclaw/wellbeing/log`) |
| user-emotion-detection | ✅ | Maps facial `emotion.detected` → mood signal |
| face-enroll | ✅ | Face enrollment qua `/face/enroll` API |
| speaker-recognizer | ✅ | Voice self-enrollment, speaker identification |
| guard | ✅ | Guard mode toggle, dramatic stranger reactions, Telegram broadcast |
