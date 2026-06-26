# OS Server API — Tài Liệu

> OS Server (Go, Gin framework) chạy trên port 5000.

## OS Server Endpoints (Go, :5000)

### Health

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/health/live` | Liveness probe |
| GET | `/api/health/readiness` | Readiness probe (agent gateway connected?) |

### System

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/system/info` | CPU, RAM, temp, uptime, version, trạng thái agent (name/connected/emotion/version/uptime) |
| GET | `/api/system/network` | WiFi SSID, IP, signal, internet status |
| GET | `/api/system/dashboard` | Snapshot tổng hợp (agent + config + HW) |

### Device Setup

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/device/setup` | Cấu hình WiFi + LLM + channel + MQTT (async, trả về ngay) |
| POST | `/api/device/channel` | Thay đổi messaging channel |

### Device Timezone (Múi giờ)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/device/timezone` | IANA zone hiện tại + danh sách zone chọn được (admin-gated) |
| POST | `/api/device/timezone` | Áp dụng một IANA zone (admin-gated) |

**GET response** (`data`):
```json
{
  "current": "Asia/Ho_Chi_Minh",
  "zones": ["UTC", "Asia/Ho_Chi_Minh", "..."]
}
```

- `current` được đọc trực tiếp (live) từ `/etc/timezone`, fallback sang resolve symlink `/etc/localtime`, rồi tới field `timezone` trong `config/config.json`.
- `zones` lấy từ `timedatectl list-timezones`, fallback sang quét `/usr/share/zoneinfo`, rồi tới danh sách common có sẵn (built-in).

**POST request body:**
```json
{ "timezone": "Asia/Ho_Chi_Minh" }
```

Zone được validate dựa trên `/usr/share/zoneinfo`; zone không tồn tại trả về HTTP 400. Khi thành công, server: trỏ lại symlink `/etc/localtime` về file tzdata của zone, ghi `/etc/timezone` (kiểu Debian, có newline cuối), chạy `timedatectl set-timezone <tz>` best-effort (không fatal nếu thiếu lệnh), và lưu `timezone` vào `config/config.json`.

Thay đổi có hiệu lực **mà KHÔNG cần restart HAL** — các clock helper của HAL (`os/hal/clock.py`) đọc lại `/etc/timezone` mỗi lần gọi.

Config field: `timezone` trong `config/config.json` (chuỗi IANA zone, omitempty) — bản ghi của zone đã áp dụng. Các file OS (`/etc/timezone` + `/etc/localtime`) mới là source of truth.

### Network

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/network` | Quét WiFi networks |
| GET | `/api/network/current` | SSID + IP hiện tại |
| GET | `/api/network/check-internet` | Kiểm tra kết nối internet |

### Guard Mode (Chế độ canh gác)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/guard/enable` | Bật chế độ canh gác |
| POST | `/api/guard/disable` | Tắt chế độ canh gác |
| GET | `/api/guard` | Kiểm tra trạng thái guard mode (trả về `{"guard_mode": true/false}`) |
| POST | `/api/guard/alert` | Gửi cảnh báo thủ công đến tất cả chat session OpenClaw |

**Request body cảnh báo:**
```json
{
  "message": "Phát hiện người lạ trong phòng khách",
  "image": "<base64 JPEG, optional>"
}
```

Khi guard mode BẬT, các sự kiện `presence.enter` và `motion` được gửi thêm đến TẤT CẢ chat session OpenClaw (Telegram DM + group) qua `chat.send` RPC. Flow sensing bình thường (emotion, servo, TTS) vẫn hoạt động không thay đổi.

Config field: `guard_mode` trong `config/config.json` (bool, mặc định `false`). OpenClaw agent cũng có thể bật/tắt guard mode qua skill `guard`.

### Sensing

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/sensing/event` | Nhận sensing event từ HAL |
| POST | `/api/mood/log` | Ghi mood user (agent gọi qua Mood skill) |
| POST | `/api/monitor/event` | Push event trực tiếp vào monitor bus (dùng bởi HAL để gửi trạng thái sound tracker) |

> **Ghi chú:** Theo dõi stranger (stats, lưu trữ) được xử lý bởi **HAL** (port 5001) tại `GET /face/stranger-stats`. Xem [sensing-behavior_vi.md](sensing-behavior_vi.md#theo-dõi-người-lạ-stranger-visit-tracking) để biết chi tiết.

**Request body:**
```json
{
  "type": "voice_command|voice|web_chat|motion|sound|presence.enter|presence.leave|presence.away|light.level|motion.activity",
  "message": "...",
  "image": "<base64 JPEG, optional>"
}
```

**Event types:**

| Type | Nguồn | Có ảnh? | Mô tả |
|------|-------|---------|-------|
| `voice_command` / `voice` | Mic (Deepgram STT) | Không | Lệnh giọng nói |
| `web_chat` | Web Monitor `/chat` UI | Có (file/clipboard attach) | Tin nhắn gõ từ web monitor — TTS suppressed (reply hiện trong UI), không wake đèn vật lý, không opening filler |
| `motion` | Camera (frame diff) | Có (large motion) | Phát hiện chuyển động |
| `presence.enter` | Camera (InsightFace recognition) | Có (JPEG bbox-annotated) | Phát hiện khuôn mặt — phân loại friend hoặc stranger |
| `presence.leave` | Camera (3 tick liên tục không thấy mặt) | Không | Người rời đi |
| `light.level` | Camera (mean brightness) | Không | Ánh sáng môi trường thay đổi đáng kể (>30/255) |
| `sound` | Mic (RMS energy) | Không | Tiếng động lớn |
| `presence.away` | PresenceService (15 phút không chuyển động) | Không | Không ai xung quanh 15+ phút — thiết bị đi ngủ |
| `motion.activity` | MotionPerception (khi PRESENT) | Không | Phát hiện hoạt động khi user có mặt — emotional actions được ghi qua Mood skill |

**Flow xử lý:**
1. `voice_command` hoặc `voice` + local intent enabled → match intent → thực thi trực tiếp (~50ms). `web_chat` skip local intent (text gõ ≠ wake-word voice).
2. Không match → forward OpenClaw qua WebSocket `chat.send`
3. Nếu event có `image` → gọi `SendChatMessageWithImage` → gửi ảnh kèm text cho AI vision phân tích. Với `web_chat`, ảnh attach được lưu vào `/tmp/web-chat-*.jpg` và gắn tag `[image: <path>]` để agent reference (vd: face enrollment).
4. `web_chat` runs được mark qua `MarkWebChatRun(runID)` để SSE handler suppress TTS lúc lifecycle end — reply chỉ hiện trong web UI.

### OpenClaw

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/openclaw/status` | Trạng thái kết nối WS; gồm `uptime` (uptime WS phía OS server) và `agentUptime` (uptime tiến trình OpenClaw, không reset khi OS server restart) |
| GET | `/api/openclaw/events` | SSE stream events real-time |
| GET | `/api/openclaw/recent` | 100 events gần nhất (ring buffer) |

---

## HAL Endpoints (Python FastAPI, :5001)

Truy cập qua nginx proxy: `/hw/*` → `127.0.0.1:5001`

### Servo (5 trục Feetech)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/servo` | Recordings + animation state |
| POST | `/servo/play` | Phát animation (idle, curious, nod, headshake, happy_wiggle, sad, excited, shock, shy, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching). Idle tự chạy khi boot. |
| POST | `/servo/move` | Gửi joint positions với smooth interpolation |
| POST | `/servo/release` | Tắt torque tất cả servo |
| GET | `/servo/position` | Vị trí servo hiện tại |
| GET | `/servo/aim` | Danh sách aim directions |
| POST | `/servo/aim` | Aim đầu thiết bị (center, desk, wall, left, right, up, down, user) |
| GET | `/servo/track/targets` | Danh sách target gợi ý cho YOLOWorld |
| POST | `/servo/track` | Bắt đầu tracking — `{"target":"cup"}` (tự detect) hoặc `{"bbox":[x,y,w,h]}`. Xem [vision-tracking_vi.md](vision-tracking_vi.md) |
| POST | `/servo/track/stop` | Dừng phiên tracking |
| GET | `/servo/track` | Trạng thái tracking (active, target, bbox, confidence) |
| POST | `/servo/track/update` | Khởi tạo lại tracker với bbox mới |

### LED (64 WS2812, grid 8x5)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/led` | LED strip info |
| GET | `/led/color` | Màu LED hiện tại |
| POST | `/led/solid` | Fill toàn bộ 1 màu |
| POST | `/led/paint` | Set từng pixel (array tối đa 64) |
| POST | `/led/off` | Tắt tất cả LED |
| POST | `/led/effect` | Bật effect (breathing, candle, rainbow, notification_flash, pulse) |
| POST | `/led/effect/stop` | Dừng effect |

### Camera

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/camera` | Availability + resolution |
| GET | `/camera/snapshot` | Chụp 1 frame JPEG. `?save=true` lưu file timestamp, trả JSON `{"path":"..."}` |
| GET | `/camera/stream` | MJPEG live stream (downscaled + throttled) |

### Audio

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/audio` | Audio device availability |
| POST | `/audio/volume` | Set volume (0-100%) |
| GET | `/audio/volume` | Get volume |
| POST | `/audio/play-tone` | Phát test tone |
| POST | `/audio/record` | Thu âm WAV |
| POST | `/audio/play` | Phát nhạc theo query. Body: `{"query":"tên bài","person":"tên"}`. `person` tuỳ chọn — lưu lịch sử theo người. Trước khi yt-dlp resolve sẽ phát một câu TTS ngắn cached ("On it.", "Coming up.", …) để thiết bị không im lặng trong lúc ffmpeg load. Bỏ qua câu này khi loa đang mute, TTS đang nói, nhạc đang phát, hoặc VoiceService đang giữa session STT. |
| POST | `/audio/stop` | Dừng phát nhạc |
| GET | `/audio/status` | Trạng thái phát nhạc (đang phát, tên bài, thời gian) |
| GET | `/audio/history` | Lịch sử phát nhạc. Query: `?person=tên&date=YYYY-MM-DD&last=50`. `person` lọc theo người; bỏ trống = shared. |

### Emotion

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/emotion` | Biểu cảm kết hợp servo + LED + display eyes |

15 emotions: curious, happy, sad, thinking, idle, excited, shy, shock, listening, laugh, confused, sleepy, greeting, acknowledge, stretching

### Scene

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/scene` | Danh sách scene presets |
| POST | `/scene` | Kích hoạt scene (reading, focus, relax, movie, night, energize) |

### Presence

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/presence` | State hiện tại (present/idle/away) |
| POST | `/presence/enable` | Bật auto presence control |
| POST | `/presence/disable` | Tắt auto presence (manual mode) |

### Face (đăng ký người quen / friend)

Cần sensing có camera (InsightFace). Mặc định ảnh người đã đăng ký lưu tại `/root/local/users/{label}/`; có thể ghi đè bằng `HAL_USERS_DIR`. Mỗi thư mục người dùng chứa `metadata.json` với `telegram_username` và `telegram_id` để gửi DM.

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/face/enroll` | Body: `image_base64`, `label`, `telegram_username`?, `telegram_id`? — lưu ảnh, train embedding, lưu Telegram identity |
| GET | `/face/status` | `enrolled_count`, `enrolled_names` |
| POST | `/face/remove` | Body: `label` — xóa một người đã đăng ký (404 nếu không có) |
| POST | `/face/reset` | Xóa toàn bộ người đã đăng ký và ảnh trên đĩa |

### User (dữ liệu per-user)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/user/info?name=X` | Metadata user: `name`, `is_friend`, `telegram_id`, `telegram_username`. Mặc định `"unknown"` nếu thiếu name. Tự tạo folder. |

> Wellbeing activity history giờ nằm trên OS server HTTP API (port 5000). Xem `POST /api/wellbeing/log` và `GET /api/openclaw/wellbeing-history` — entries ghi JSONL tại `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` với schema `{ts, seq, hour, action, notes}` (action ∈ `drink`/`break`/`sedentary`/`emotional`). HAL không còn host endpoint wellbeing.

### Display (GC9A01 1.28" LCD tròn)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/display` | State hiện tại (mode, expression) |
| POST | `/display/eyes` | Set eye expression + pupil position |
| POST | `/display/info` | Chuyển sang info mode (text/subtitle) |
| POST | `/display/eyes-mode` | Chuyển về eyes mode (default) |
| GET | `/display/snapshot` | Frame hiện tại dưới dạng JPEG |

11 expressions: neutral, happy, sad, curious, thinking, excited, shy, shock, sleepy, angry, love

### Voice

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/voice/start` | Start voice pipeline (Deepgram STT + TTS) |
| POST | `/voice/stop` | Stop voice pipeline |
| POST | `/voice/speak` | TTS — chuyển text thành giọng nói. Body fields: `text`, `voice?`, `interruptible?`, `provider?`, `tts_api_key?`, `tts_base_url?`, `cached?` (dùng WAV cache, render+save khi miss), `prerender?` (render+save không play — warmup lúc boot) |
| GET | `/voice/status` | voice_available, voice_listening, tts_available, tts_speaking |

### System

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/health` | Hardware driver availability |

---

## Response Format

OS Server (Go):
```json
{"status": 1, "data": {...}, "message": null}   // success
{"status": 0, "data": null, "message": "error"}  // failure
```

HAL (Python): FastAPI standard JSON responses.

## Startup

1. OS Server khởi động Gin trên :5000
2. Đọc `config/config.json`
3. Nếu `SetUpCompleted`:
   - Kết nối OpenClaw WebSocket
   - Kết nối MQTT
   - Start ambient behaviors
4. Nếu chưa setup: chờ `POST /api/device/setup`

## Local Intent Matching

Khi nhận `voice_command` hoặc `voice` event, OS server check local intent trước (~50ms):

| Lệnh | Hành động |
|-------|-----------|
| "bật đèn", "turn on light" | `/led/solid` warm + happy emotion |
| "tắt đèn", "turn off light" | `/led/off` + idle emotion |
| "đọc sách", "reading mode" | scene:reading |
| "tập trung", "focus mode" | scene:focus |
| "thư giãn", "relax" | scene:relax |
| "xem phim", "movie mode" | scene:movie |
| "đèn ngủ", "goodnight" | scene:night + sleepy emotion |
| "sáng lên", "brighter" | scene:energize |
| "vui lên", "happy" | emotion:happy |
| "buồn", "sad" | emotion:sad |
| "tăng âm", "volume up" | volume 80 |
| "giảm âm", "volume down" | volume 30 |
| "im", "mute" | volume 0 |

Không match → forward OpenClaw.
