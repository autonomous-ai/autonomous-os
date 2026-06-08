# Open Decisions — AI Lamp (Lamp)

> Decisions that must be made before implementation can proceed. Each blocks specific features.
> Once resolved, move to "Resolved" section with the decision and date.

## Unresolved

| Decision | Context | Options | Recommendation |
|---|---|---|---|
| Channel abstraction layer | UC-15 multi-channel (Telegram/Slack/Discord) currently "free" via OpenClaw built-in support. But if gateway is swapped to a non-OpenClaw provider, multi-channel breaks. | (1) Do nothing — accept OpenClaw lock-in for channels. (2) Build a channel abstraction at Lamp level so messaging works regardless of gateway. (3) Defer until gateway swap is actually planned. | Option 3 for now (YAGNI), but design UC-15 with awareness that channel support is gateway-dependent. Document the dependency explicitly. |

---

## Resolved (2026-03-24)

| Decision | Resolution | Docs |
|---|---|---|
| Go-to-Python bridge protocol | HTTP proxy. LeLamp FastAPI on `127.0.0.1:5001`, Lamp proxies from port 5000. | `architecture-decision.md` §11, `bootstrap-ota.md` §6 |
| LeLamp source strategy | Mono-repo. Copy drivers from `humancomputerlab/lelamp_runtime` into `lelamp/`. Track upstream via `UPSTREAM.md`. | `bootstrap-ota.md` §6 |
| Project/character name | **Lamp** (from "luminous"). Binary: `lamp-server`. Service: `lamp.service`. Wake word: "Hey Lamp". | All docs updated |
| Display concept | Dual-mode: pixel-art eyes emotion (default) + info display (time, weather, timer, notifications). | `architecture-decision.md` §3, `product-vision.md` §4 |
| Autonomous sensing architecture | Hybrid. Lamp runs lightweight edge detection (camera, mic, time, sensors). Pushes events to OpenClaw for AI decisions. Rule-based actions don't need AI. | `product-vision.md` §2 Pillar 4, `architecture-decision.md` §4 |
| OTA components | 5 components: lamp, bootstrap, web, openclaw, lelamp. LeLamp = setup stage 2b. | `bootstrap-ota.md` §1-§3 |
| Product pillars | 4 Pillars: "It understands me", "It feels alive", "It's actually useful", "It acts on its own". | `product-vision.md` §2 |

## Resolved (2026-03-25)

| Decision | Resolution | Docs |
|---|---|---|
| GWS removal | Removed all GWS (Google Workspace) handlers, scripts, and domain types. MQTT commands reduced to: `info`, `add_channel`, `ota`. | `architecture-decision.md` §4 |
| LLM service inlining | Removed `internal/llm/` service. `ListModelsFromAPI` inlined into `internal/openclaw/service.go`. | `architecture-decision.md` §4 |
| Onboarding removal | Removed `onboarding.go` from openclaw. Setup flow simplified. | `architecture-decision.md` §4 |
| Scripts cleanup | Removed `release-*.sh`, `setup-gws-cli.sh`, `upload-gws-cli.sh`, `install-sendip.sh`, `sendip.sh`. Added `upload-lelamp.sh`. | `bootstrap-ota.md` §7 |
| Code directory rename | All code moved under `lamp/` subdirectory. "intern" references replaced with "lamp". | All docs |
| LED driver ownership | LeLamp Python rpi_ws281x owns all LED control. Go SPI driver (`internal/led/`) removed entirely — this lamp's hardware uses LeLamp's LED driver exclusively. No SPI bus conflict. | `architecture-decision.md` §3, §4, §9, §11 |
| SKILL.md content (#1) | 8 SKILL.md files: led-control, servo-control, camera, audio, emotion, sensing, scene, display, scheduling. All describe HTTP API at `127.0.0.1:5001`. | `resources/openclaw-skills/` |
| OpenClaw event push (#2) | WebSocket RPC `chat.send` with `operator.write` scope. LeLamp POST → Lamp Go `/api/sensing/event` → OpenClaw WS. | `server/sensing/delivery/http/handler.go` |
| Camera processing (#3) | On-device OpenCV in LeLamp Python. Frame diff for motion, Haar cascade for face detection, mean brightness for light level. Auto-snapshot (full-resolution JPEG q85) on significant events → forwarded to OpenClaw with vision. | `lelamp/service/sensing/sensing_service.py` |
| AI Vision | Enabled (`SupportsVision: true`, `Input: ["text", "image"]`). Sensing events with images sent via `SendChatMessageWithImage` → OpenClaw LLM can see camera snapshots. | `lamp/internal/openclaw/service.go` |
| Face detection vs recognition | **Both done.** Face detection (P1) via Haar cascade. Face **recognition** (P2) via InsightFace embeddings — `facerecognizer.py` classifies owners vs strangers, fires `presence.enter` with name or `stranger_N`. Enrollment via `/face/enroll` API + `face-enroll/SKILL.md`. Stranger visit tracking with persistence. | `facerecognizer.py`, `face-enroll/SKILL.md`, `sensing_service.py` |
| Voice/speaker identification | **Done (2026-04).** LeLamp `speaker_recognizer.py` identifies speakers by voice embedding. Transcripts prefixed with `Name:` (known) or `Unknown:` (not enrolled). Self-enrollment via voice intro or Telegram voice note. Profiles stored per-user alongside face data. | `lelamp/speaker_recognizer.py`, `speaker-recognizer/SKILL.md` |
| Enrolled gating strategy | **Done (2026-04).** Dual-gate: face recognition (InsightFace, `facerecognizer.py`) + voice recognition (`speaker_recognizer.py`). Both run on-device. Unrecognized faces/voices classified as `Unknown`/`stranger`. Per-user data gated by identity. Self-enrollment for both face (`/face/enroll`) and voice (via skill). | `lelamp/service/sensing/perceptions/facerecognizer.py`, `face-enroll/SKILL.md`, `speaker-recognizer/SKILL.md` |
| Audio input ownership (#4) | LeLamp owns mic. Local VAD gates Deepgram connection (cost saving). Sensing loop also taps mic for ambient sound level (shared). | `lelamp/service/voice/voice_service.py` |
| Emotion presets (#6) | 8 presets implemented: curious, happy, sad, thinking, idle, excited, shy, shock. Each maps to servo recording + LED color + eye expression. | `lelamp/server.py` EMOTION_PRESETS |
| Display rendering (#7) | `gc9a01-python` driver + PIL/Pillow rendering. 11 eye expressions drawn with ImageDraw. Dual-mode: eyes (default) + info text. Background render loop with auto-blink. | `lelamp/service/display/` |
| Voice pipeline | Local VAD (RMS energy) + on-demand Deepgram STT. Mic always on, Deepgram only when speech detected. Wake word "Hey Lamp" detected in transcript → `voice_command` event (priority). No wake word → `voice` event (ambient sensing). | `lelamp/service/voice/voice_service.py` |
| Lighting scenes | 6 presets: reading, focus, relax, movie, night, energize. Simulated color temp via RGB. | `lelamp/server.py` SCENE_PRESETS |
| Presence auto-control | State machine: PRESENT → IDLE (5min) → AWAY (15min). Motion restores light. | `lelamp/service/sensing/presence_service.py` |
| Scheduling/timers | OpenClaw built-in cron (enabled by default). SKILL.md teaches LLM to use `cron.add`. No custom code needed. | `resources/openclaw-skills/scheduling/SKILL.md` |

| AGENTS.md | Use OpenClaw default. Custom rules to be tuned after Pi testing. | N/A |

---

## Implementation Status

### P0 — First Prototype (code done, needs Pi testing)

- **UC-01 Voice-Controlled Lighting** ✅ — Deepgram STT → OpenClaw → SKILL.md → LED
- **UC-02 Color & Color Temp** ✅ — `/led/solid`, `/led/paint`, scene presets
- **UC-14 Audio Feedback** ✅ — TTS `/voice/speak`, volume, play-tone

### P1 — v1.0 (code done)

- **UC-03 Scene/Mood Presets** ✅ — 6 scenes (reading, focus, relax, movie, night, energize)
- **UC-04 Timer & Schedule** ✅ — OpenClaw built-in cron + `scheduling/SKILL.md`
- **UC-06 AI Companion** ✅ — OpenClaw + SOUL.md + emotion + long-term memory
- **UC-08 Servo Direction** ✅ — `/servo/play`, 8 animations
- **UC-11 Presence Detection** ✅ — Sensing loop + face detection (Haar cascade) + presence.enter/leave events + presence state machine (auto on/dim/off) + light level detection + auto-snapshot with AI vision
- **UC-13 Status Indication** 🟡 — Partial (boot/error states, needs processing/timer/OTA)

### P2 — v1.x (partially started)

- UC-05 Circadian Lighting
- UC-07 Light Effects ✅ (partial) — Breathing LED + color drift implemented in `internal/ambient/` as idle behaviors. Rainbow/candle effects not yet implemented.
- UC-09 Auto-Tracking (camera → servo follow)
- UC-10 Gesture Control
- UC-12 Video Call Optimization
- UC-15 Remote Control — **Note:** Telegram/Slack/Discord currently provided by OpenClaw built-in multi-channel (zero Lamp code needed). If gateway is changed, Lamp needs its own channel abstraction layer. See Unresolved decisions.
- Face Recognition ✅ — InsightFace embeddings, owner/stranger classification, `/face/enroll` + `face-enroll/SKILL.md`
- Voice/Speaker Recognition ✅ — LeLamp `speaker_recognizer.py`, voice embedding profiles, self-enrollment via voice intro or Telegram voice note
- Facial Emotion Detection ✅ — dlbackend WS emotion classifier, `emotion.detected` events, `user-emotion-detection/SKILL.md`
- Proactive Wellness ✅ — `wellbeing/SKILL.md`, event-driven from `motion.activity` sedentary labels
- Proactive Music Suggestion ✅ — `music-suggestion/SKILL.md`, mood + sedentary triggers

### 4 Pillars — All Have Code ✅

| Pillar | Status | Implementation |
|---|---|---|
| 1. "It understands me" | ✅ | OpenClaw + SOUL.md + long-term memory |
| 2. "It feels alive" | ✅ | Servo + LED + emotion + display eyes (11 expressions, auto-blink) |
| 3. "It's actually useful" | ✅ | Scenes, scheduling (cron), voice assistant |
| 4. "It acts on its own" | ✅ | Sensing loop (motion + sound) + presence auto on/off + ambient idle behaviors (breathing LED, color drift, servo micro-movements, TTS self-talk) |

### Skills (18 total) ✅

| Skill | Endpoints / Description |
|---|---|
| led-control | `/led/solid`, `/led/paint`, `/led/off` |
| servo-control | `/servo`, `/servo/play` |
| camera | `/camera`, `/camera/snapshot`, `/camera/stream` |
| audio | `/audio`, `/audio/volume`, `/audio/play-tone`, `/audio/record` |
| emotion | `/emotion` (coordinates servo + LED + display eyes) |
| sensing | Auto — motion/sound/presence/light events → OpenClaw (with vision) + presence auto-control |
| sensing-track | Activity/presence timeline tracking via wellbeing JSONL |
| scene | `/scene` (6 lighting presets) |
| display | `/display/eyes`, `/display/info`, `/display/snapshot` |
| voice | Voice pipeline control, TTS routing |
| music | Reactive music playback (`/audio/play`, `/audio/stop`) |
| music-suggestion | Proactive music suggestion (mood + sedentary triggers) |
| mood | Mood signal fusion + decision logging (`/api/mood/log`) |
| wellbeing | Event-driven hydration/break reminders (`/api/openclaw/wellbeing/log`) |
| user-emotion-detection | Maps facial `emotion.detected` → mood signal |
| face-enroll | Face enrollment via `/face/enroll` API |
| speaker-recognizer | Voice self-enrollment, speaker identification |
| guard | Guard mode toggle, dramatic stranger reactions, Telegram broadcast |
