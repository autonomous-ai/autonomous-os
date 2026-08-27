# Tổng Quan Kiến Trúc — Autonomous

## Kiến Trúc 3 Tầng

```
Agentic Runtime (AI/LLM) → OS Server (Go, :5000) → HAL (Python, :5001) → Phần cứng
```

| Tầng | Ngôn ngữ | Port | Vai trò |
|------|----------|------|---------|
| Agentic Runtime | Go | WS | Bộ não AI, LLM, SKILL.md, memory, channels |
| OS Server | Go | 5000 | Hệ thống (mạng, OTA, MQTT, reset), sensing event routing, local intent |
| HAL | Python | 5001 | Hardware drivers (servo, LED, camera, audio, display), FastAPI |

## Thư Mục Dự Án

```
system/
├── cmd/os-server/main.go              — Entry point OS Server
├── cmd/bootstrap/main.go         — OTA bootstrap worker
├── server/
│   ├── server.go                 — Gin HTTP server, route setup
│   ├── config/                   — JSON config management
│   ├── health/delivery/http/     — Health, system info, dashboard
│   ├── network/delivery/http/    — WiFi scan, connect
│   ├── device/delivery/          — Setup (HTTP + MQTT handlers)
│   ├── sensing/delivery/http/    — Sensing event → intent match / agent gateway
│   └── openclaw/delivery/sse/    — OpenClaw status, SSE events
├── agent/  ambient/  beclient/  buddy/  device/  healthwatch/
├── intent/  monitor/  network/  skills/  statusled/  vision/
│                                 — System managers, one folder per diagram chip
├── lib/mqtt/                     — MQTT client (Eclipse Paho autopaho)
├── domain/                       — Shared structs
├── bootstrap/                    — OTA worker
└── web/                          — React 19 + Vite + Tailwind CSS 4 SPA

runtimes/                   — Swappable brains: openclaw/ hermes/ picoclaw/ codex/ claudecode/ opencode/

hal/
├── server.py                     — FastAPI server
├── config.py                     — Hằng số runtime (ngưỡng sensing, timeout, URL)
├── board/                        — Profile thiết bị, pin map board và overlay
├── drivers/                      — Service phần cứng (camera, motor, RGB, sensing, voice, display)
├── routes/                       — Module FastAPI route theo capability
├── safety/                       — Safety policy đã parse và gate tất định
├── realtime/                     — Realtime voice agent và context manager
├── server_support/               — HTTP/security support dùng chung
└── pyproject.toml                — Python dependencies (opencv-python, insightface)

robots/                          — Per-device configs and overlays
  contract/                       — Shared API contracts (+ cts/ compliance suite)
skills/                           — Built-in SKILL.md cho agent runtime, gồm cả
                                    skill-creator để chủ thiết bị tự tạo skill
integrations/                     — Off-device: companions/, chat-bridges/, perception-service/
```

## Nguyên Tắc

- **Hardware là plugin** — cắm vào thì play, không cắm thì skip
- **Tầng hệ thống chạy KHÔNG cần runtime** — thiết bị luôn phản hồi
- **Code là source of truth** — docs phản ánh code
- **HAL là hardware driver** — không chứa logic AI
- **SKILL.md native** — không dùng MCP, LLM tự đọc skill và gọi curl
- **Chủ thiết bị có thể tạo skill** — `skill-creator` built-in hướng dẫn soạn,
  kiểm thử và đóng gói skill để đưa lên Autonomous Skill Store.

## Lamp Simulator trên Laptop

`make sim` khởi động declaration `lamp` production trên laptop. HAL vẫn dùng
route và safety gate bình thường, nhưng thay motion, LED, camera, microphone,
speaker, voice và sensing bằng service ảo; nó không mở servo bus, camera/mic
macOS, GPIO hay gửi GELF log. Khi khởi động, lệnh in link local có thể click
cho HAL docs và, với body Lamp mặc định, `http://127.0.0.1:5001/simulator`.
Link sau có CAD assembly Lamp đã check-in để xem quanh, cùng giá trị live của
năm joint, playback recording và nút LED effect qua đúng endpoint `/servo/*`
và `/led/*` mà skill sử dụng: kéo để xoay camera, cuộn để zoom, double-click
để reset. Bảng state ghi rõ posture mode đang giữ body (`zero`, `hold`) bên
cạnh giá trị joint live, và nút Motor control đang giữ mode đó được làm sáng: một
animation bấm lúc motor đang hold sẽ hiện là bị bỏ qua kèm lý do, không phải
dấu tích, vì `/servo/play` trả `"ignored"` thay vì `"ok"` cho lệnh nó đã bỏ.
Motion preview của CAD phản hồi theo joint live, kể cả khi phát
recording; một control chuyển về assembly tĩnh nguyên bản để đối chiếu. Repo
chưa có hierarchy cơ khí, pivot, axis hay CAD zero offset đã calibration, nên
phản hồi hình ảnh này không khẳng định pose `down` hoặc `right` render ra đúng
với Lamp vật lý.

Đây là interface simulator, không phải physics model: không có khối lượng hay
va chạm; recording CSV có sẵn chỉ replay timing trong RAM. Nội dung
camera/audio ảo là deterministic. Muốn boot test body tối giản theo contract
thì chạy `make sim DEVICE_TYPE=sim`. Body đó chỉ khai `motion` và `system`,
không gì khác — đó là cách chứng minh HAL mount đúng những gì `ROBOT.md` khai
và không mount thêm; lamp không kiểm được điều này vì lamp khai đủ mọi thứ.
Xem `robots/sim/ROBOT.md`.

Muốn kiểm tra media thật trên Mac, chạy `make sim SIM_MEDIA=host`. Lệnh này
chủ động mở camera, microphone và speaker của máy: trang simulator hiển thị
camera stream, **Play test tone** dùng speaker, còn **Record 3 seconds** thu
WAV để phát lại. Mặc định `SIM_MEDIA=virtual` không xin permission và giữ test
deterministic.

Host mode không bao giờ crash. Lúc boot mỗi subsystem được probe — webcam mở và
đọc thử một frame, microphone thu vài mili-giây — cái nào thiếu, đang bị chiếm
hoặc bị từ chối permission thì tự rơi về thiết bị ảo kèm log `[sim-media]`.
`GET /simulator/state` báo kết quả theo từng subsystem (`media_camera`,
`media_audio`, `media_reasons`; `media` chỉ là "host" khi cả hai đều host), và
trang simulator in đúng lý do đó, đồng thời tắt nút tone/record khi audio là
ảo. Trên macOS, hai permission nằm ở System Settings > Privacy & Security >
Camera và Microphone, phải cấp cho ứng dụng terminal đang chạy HAL.

Camera ở host mode dùng driver chỉ-dành-cho-simulation
(`hal/drivers/camera/host_capture_device.py`, đăng ký tên `host`) mở webcam qua
backend OpenCV gốc của hệ điều hành — AVFoundation trên macOS, nơi đường V4L2
của production và phần healing power-cycle USB không tồn tại. Body thật vẫn
chọn `driver:` từ ROBOT.md.

Host mode còn chạy **pipeline giọng nói thật**: entry VAD, Silero, STT, realtime
agent (Gemini Live), wake word, và phần dispatch `[turn] route=…` forward sang
os-server — đúng code mà board chạy, không phải stub. Cổng quyết định là
`state.simulation_audio`, nên hai quyết định không bao giờ lệch nhau: laptop
đang dùng microphone của chính nó thì được đúng pipeline mà microphone đó sinh
ra, còn khi macOS từ chối permission thì cờ lật lại và rơi về
`VirtualVoiceService` kèm log lý do, thay vì dựng pipeline thật lên một thiết bị
đã chết. `SIM_MEDIA=virtual` vẫn giữ stub, nên test vẫn im lặng và offline.

Credential lấy từ chính file config.json mà HAL dùng chung với os-server, y như
trên board — trỏ `OS_CONFIG_PATH` vào state dir của `make os-dev` là một file
nuôi cả hai process. Riêng `llm_api_key` phủ cả LLM, `AutonomousSTT`, TTS
ElevenLabs, mô tả ảnh, **và cả** Gemini Live (key fallback về nó, endpoint là
`llm_base_url` + `/ws/gemini`); `deepgram_api_key` là tuỳ chọn, chỉ để đổi
provider STT.

Nhạc cũng phát được. `MusicService` stream yt-dlp → ffmpeg → **aplay** (ALSA),
hoặc **paplay** khi có sink Bluetooth — macOS không có cái nào, nên cả hai đường
đều chết ngay ở `Popen` và thiết bị nói ra "Sorry, I can't play that right now".
macOS được đường thứ ba: chính output device AudioToolbox của ffmpeg, chọn nó
thay vì `ffplay` vì ffmpeg vốn đã là dependency bắt buộc ở đây còn `ffplay` thì
không phải build nào cũng có. `SIM_MEDIA=virtual` giữ nguyên pipeline nhưng đổ
vào null sink — vẫn search và decode đầy đủ, chỉ là laptop im lặng.

Có hai đường dẫn của HAL là device-absolute và phải dời khi chạy trên laptop,
cả hai đều đã đọc từ env và được target `sim` set sẵn: `HAL_SNAPSHOT_DIR` (nơi
`GET /camera/snapshot?save=true` ghi file — bắt buộc nằm dưới home của chính
agent runtime, trên board là `/root/.codex/media/hal-snapshots`, nếu không agent
không đọc lại được frame và os-server không serve được thumbnail) và
`HAL_SNAPSHOT_PERSIST_DIR` (`/var/lib/hal/snapshots` chỉ root ghi được).

## Voice Pipeline

```
Mic (always on) → Local VAD (RMS energy, free)
    → Speech detected → Connect Deepgram STT
        → "hey lamp, tắt đèn" → voice_command → local intent → thực thi
        → "anh ơi đi ăn không" → voice (ambient) → OpenClaw
    → Silence 3s → Disconnect Deepgram
    → _submit_speech_emotion_from_session: WAV → perception-service SER → OS server event (luôn chạy, độc lập transcript)
    → _identify_and_decorate (1 lần) → if transcript: _send_to_lamp voice/voice_command
```

Chi tiết SER: [speech-emotion_vi.md](speech-emotion_vi.md).

## Sensing Flow

```
HAL sensing loop (mỗi 2s) → Đọc 1 frame camera, chạy tất cả detectors:
    ├─ Motion detection (frame diff) → event nếu >8% pixel thay đổi
    ├─ Face recognition (InsightFace buffalo_sc) → phân loại friend/stranger
    │     → presence.enter (JPEG được annotate bbox: xanh=friend, đỏ=stranger)
    │     → presence.leave (3 tick liên tiếp không thấy mặt)
    ├─ Light level (mean brightness, mỗi 30s) → event nếu thay đổi >30/255
    └─ Sound detection (mic RMS) → event nếu > threshold

Event có ảnh? (large motion, face enter) → encode frame full-resolution JPEG q85
Ảnh face enter: frame gốc được vẽ bounding box + nhãn friend/stranger

POST /api/sensing/event {type, message, image?}
    → OS server (Go):
        1. Voice event + local intent match? → thực thi trực tiếp (~50ms)
        2. Không match → forward OpenClaw:
           - Có image → SendChatMessageWithImage (text + vision content block)
           - Không image → SendChatMessage (text only)
        3. OpenClaw AI nhìn ảnh + đọc context → quyết định hành động → gọi SKILL API
```

Cooldown bảo vệ chi phí LLM: motion/sound 60s, presence 10s, light.level 30s.
