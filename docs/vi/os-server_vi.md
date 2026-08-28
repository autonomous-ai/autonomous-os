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
| GET | `/api/system/ota-security` | Trạng thái tin cậy OTA lấy từ bootstrap worker: `legacy` hay `verified`, fingerprint key đã pin, lần fetch metadata gần nhất (xem `bootstrap-ota.md`) |
| POST | `/api/system/reboot` | Cần admin auth: trả ACK, rồi yêu cầu HAL phát cue và reboot OS |
| POST | `/api/system/shutdown` | Cần admin auth: trả ACK, rồi yêu cầu HAL phát cue, release servo và shutdown OS |

Hai endpoint power trả `202 Accepted` trước khi đặt lịch gọi HAL, để trình duyệt
nhận được ACK trước lúc thiết bị không còn truy cập được. Mỗi lúc chỉ có một
reboot hoặc shutdown chờ chạy; request thứ hai nhận `409 Conflict`. HAL sở hữu
chuỗi thao tác vật lý: reboot phát cue reboot; shutdown phát cue rồi release
servo trước khi chạy lệnh power của OS.

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

Thay đổi có hiệu lực **mà KHÔNG cần restart HAL** — các clock helper của HAL (`hal/clock.py`) đọc lại `/etc/timezone` mỗi lần gọi.

Config field: `timezone` trong `config/config.json` (chuỗi IANA zone, omitempty) — bản ghi của zone đã áp dụng. Các file OS (`/etc/timezone` + `/etc/localtime`) mới là source of truth.

### Network

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/network` | Quét WiFi networks |
| GET | `/api/network/current` | SSID + IP hiện tại |
| GET | `/api/network/check-internet` | Kiểm tra kết nối internet |

**Monitor kết nối** (`system/network/service.go`, chạy khi `SetUpCompleted` = true).
Ping `8.8.8.8` mỗi 5s — không phụ thuộc interface, nên máy online qua dây vẫn được
tính là online. Fail 5 lần liên tiếp → bật LED state `Connectivity`; fail 10 lần
(~50s) → leo thang sang reconnect WiFi (restart `wpa_supplicant@wlan0`, bounce
interface); reconnect fail 5 lần (~10 phút) → reboot thiết bị.

Nấc leo thang đó là đường phục hồi dành cho **WiFi**, nên bị bỏ qua khi WiFi không
phải là link đang có vấn đề — nếu không, máy chạy dây sẽ tự reboot mỗi ~10 phút suốt
thời gian ISP hỏng mà nó chẳng liên quan. Bỏ qua khi một trong hai: không có SSID nào
được lưu (máy provision bằng dây — xem `setupWired` trong `docs/setup-flow.md`), hoặc
default route thuộc về interface khác (traffic đang đi ra bằng dây). Còn khi link WiFi
rớt thật thì *không* còn default route nào cả và `PrimaryInterface()` fallback về
`wlan0`, nên đúng sự cố mà nấc này sinh ra để xử lý vẫn lọt qua guard.

### Guard Mode (Chế độ canh gác)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/guard/enable` | Bật chế độ canh gác |
| POST | `/api/guard/disable` | Tắt chế độ canh gác |
| GET | `/api/guard` | Kiểm tra trạng thái guard mode (trả về `{"guard_mode": true/false}`) |
| POST | `/api/guard/alert` | Gửi cảnh báo thủ công đến tất cả chat session OpenClaw |

Mọi guard endpoint yêu cầu xác thực quản trị với caller từ mạng. Caller nội bộ
qua strict loopback, gồm HAL và agent runtime, vẫn được phép để guard mode nội
bộ tiếp tục hoạt động.

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

> **Ghi chú:** Theo dõi stranger (stats, lưu trữ) được xử lý bởi **HAL** (port 5001) tại `GET /face/stranger-stats`. Xem [sensing-behavior_vi.md](../../robots/lamp/docs/vi/sensing-behavior_vi.md#theo-dõi-người-lạ-stranger-visit-tracking) để biết chi tiết.

**Request body:**
```json
{
  "type": "voice_command|voice_followup|voice|web_chat|mqtt_chat|motion|sound|presence.enter|presence.leave|presence.away|light.level|motion.activity",
  "message": "...",
  "image": "<base64 JPEG, optional>"
}
```

**Event types:**

| Type | Nguồn | Có ảnh? | Mô tả |
|------|-------|---------|-------|
| `voice_command` / `voice_followup` / `voice` | Mic (Deepgram STT) | Không | `voice_command` đã xác nhận wake word; `voice_followup` được cửa sổ focus wake word cho phép; `voice` là STT ambient |
| `web_chat` | Web Monitor `/chat` UI | Có (file/clipboard attach) | Tin nhắn gõ từ web monitor — TTS suppressed (reply hiện trong UI), không wake đèn vật lý, không opening filler |
| `mqtt_chat` | MQTT `kind:"chat.send"` (app điện thoại) | Có (image + file) | Xử lý y hệt `web_chat` ở mọi gate (`sensingmsg.IsChat`); tách type chỉ để badge Flow Monitor hiện đúng nguồn. `speak:true` thì forward thành `voice` |
| `motion` | Camera (frame diff) | Có (large motion) | Phát hiện chuyển động |
| `presence.enter` | Camera (InsightFace recognition) | Có (JPEG bbox-annotated) | Phát hiện khuôn mặt — phân loại friend hoặc stranger |
| `presence.leave` | Camera (3 tick liên tục không thấy mặt) | Không | Người rời đi |
| `light.level` | Camera (mean brightness) | Không | Ánh sáng môi trường thay đổi đáng kể (>30/255) |
| `sound` | Mic (RMS energy) | Không | Tiếng động lớn |
| `presence.away` | PresenceService (15 phút không chuyển động) | Không | Không ai xung quanh 15+ phút — thiết bị đi ngủ |
| `motion.activity` | MotionPerception (khi PRESENT) | Không | Phát hiện hoạt động khi user có mặt — emotional actions được ghi qua Mood skill |

**Flow xử lý:**
1. `voice_command`, `voice_followup` hoặc `voice` + local intent enabled → match intent → thực thi trực tiếp (~50ms). `voice_followup` có cùng độ ưu tiên người dùng như `voice_command`; `web_chat` / `mqtt_chat` skip local intent (text gõ ≠ wake-word voice).
2. Ambient turn floor: `motion.activity`, `emotion.detected`, `speech_emotion.detected`, `sound`, `presence.away`, `light.level` bị drop khi agent turn gần nhất mà handler này tạo (bất kể type) cách đây chưa tới `sensing_turn_floor_s` giây (key config, mặc định `120`, `0` = tắt; guard mode bypass). Một floor xuyên-type đè trên các gate per-type độc lập của HAL — một loạt event khác type chỉ tốn tối đa 1 agent turn mỗi window. Event bị drop hiện thành `sensing_drop` (reason `ambient_floor`) trong Flow Monitor.
3. Không match → forward OpenClaw qua WebSocket `chat.send`
4. Nếu event có `image` → gọi `SendChatMessageWithImage` → gửi ảnh kèm text cho AI vision phân tích. Với type chat (`web_chat` / `mqtt_chat`), ảnh attach được lưu vào `/tmp/web-chat-*.jpg` và gắn tag `[image: <path>]` để agent reference (vd: face enrollment).
5. Run chat (`web_chat` / `mqtt_chat`) được mark qua `MarkWebChatRun(runID)` để SSE handler suppress TTS lúc lifecycle end — reply chỉ hiện trong UI chat (web SSE, hoặc stream MQTT `chat.event`).

### OpenClaw

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/agent/status` | Trạng thái kết nối WS; gồm `uptime` (uptime WS phía OS server) và `agentUptime` (uptime tiến trình OpenClaw, không reset khi OS server restart) |
| GET | `/api/agent/events` | SSE stream events real-time |
| GET | `/api/agent/recent` | 100 events gần nhất (ring buffer) |
| POST | `/api/agent/speech/cancel` | Cử chỉ huỷ vật lý (single click, do HAL gọi — auth loopback-only để nút vẫn chạy khi chưa login). Bịt miệng mọi turn đang chạy và dừng playback ở HAL (`StopTTS`, đồng thời xoá luôn hàng đợi speak đã pre-synth). **Không** abort turn: turn vẫn chạy tiếp, tool vẫn fire, text vẫn về web chat và history — chỉ mất quyền dùng loa. Cài đặt bằng một watermark unix-ms đơn điệu (`speechWatermarkMs`): `deliverTTS` bỏ mọi câu trả lời thuộc turn được tạo tại hoặc trước mốc, kèm flow event `tts_cancelled`. Tuổi của turn đọc từ runID — id thiết bị kết thúc bằng timestamp tạo (`device-chat-7-<unix-ms>`, 13 chữ số), id kênh (`tg-<messageID>`) không có nên fallback về thời điểm đầu tiên run đó xin nói. Vì turn mới luôn nằm phía sau mốc, user click xong nói ngay được trong khi backlog cũ chạy nốt trong im lặng; watermark không bao giờ cần xoá. Cùng cái mốc đó cũng chặn luôn marker `[HW:]` của turn tại `fireHWCall` — servo và LED dừng theo, vì thiết bị vẫn cựa quậy sau khi bị bảo dừng thì user đọc là "nó phớt lờ mình". runID được đưa qua `resolveRunID` trước: đường TTS đã cầm id thiết bị trong khi đường HW có thể còn cầm UUID gốc của backend cho CÙNG một turn, và phán riêng lẻ thì câu trả lời bị bịt trong khi marker vẫn fire. Riêng `/dm`, `/broadcast`, `/speak` được miễn (cổng chặn đặt sau chúng): click nghĩa là "đừng nói với tôi", không được nuốt câu trả lời gửi cho user Telegram. Cú click cũng gọi `FillerManager.CancelAllActive()`. Filler nói thẳng xuống HAL, không đi qua `deliverTTS`, nên watermark một mình không với tới được — mà turn bị bịt tiếng thì vẫn chạy tiếp, nên mỗi lần nó xong một tool là lại re-arm thêm một câu "một giây nhé" cho một câu trả lời user vừa huỷ. Mọi run đang giữ trạng thái filler tại thời điểm đó đều nằm phía cũ của mốc nên bị bỏ hết; filler Opening của câu user nói TIẾP THEO được arm sau đó nên không bị ảnh hưởng. Câu trả lời bị bỏ vẫn được POST sang `POST /voice/realtime/history` của HAL: cú click lấy đi cái loa chứ không lấy đi câu trả lời, mà bản ghi của realtime về những gì agent chính đã đáp vốn treo ở lúc TTS phát xong (xem `docs/realtime-voice.md`). |
| POST | `/api/agent/restart` | Recovery "start + enable + restart" cho runtime đang active. Các bước: (1) best-effort `systemctl enable <unit>` — `<unit>` lấy từ map runtime→unit (`openclaw`, `hermes-gateway`, `picoclaw`, `codex`, `claudecode`, `opencode`) — để fix vẫn còn sau reboot; (2) `agentGateway.RestartAgent()` gọi `systemctl restart <unit>` — tự START service ngay cả khi đang stopped. Response `{backend, enabled}`. Dùng bởi card Agent Gateway ở Overview để phục hồi gateway đã stopped+disabled, không cần SSH. Các caller restart nội bộ (config refresh, migration) vẫn bỏ qua bước enable. |

---

## Device Ops Alerts (gửi ra → bff-campaign-service)

Thiết bị gửi **cảnh báo vận hành / bảo trì về chính hành động của nó** tới
`POST {llm_base_url}/alert` (tức `/api/v1/ai/v1/alert` trên bff-campaign-service),
xác thực bằng lobster API key của thiết bị (`Authorization: Bearer <llm_api_key>`).
bff-campaign-service giữ Telegram bot token + chat đích và chuyển tiếp nội dung
tới một chat bảo trì cố định — token **không bao giờ nằm trên thiết bị hay trong
repo public này**. Cài đặt trong `system/lib/alert`.

**Phạm vi dữ liệu & quyền riêng tư:** các cảnh báo này chỉ báo cáo **hành động và
thay đổi trạng thái của thiết bị** — không bao giờ chứa nội dung của khách hàng.
Không thu thập tin nhắn chat, không dữ liệu cá nhân. Chúng chỉ phục vụ **cải thiện
sản phẩm và troubleshooting**. Mỗi cảnh báo kèm định danh thiết bị (label, MAC,
SSID, IP, version các thành phần) cùng kết quả hành động bên dưới.

**Sự kiện kích hoạt cảnh báo:**

| Sự kiện | Trigger |
|---------|---------|
| Đổi runtime | `hermes.setup` / `picoclaw.setup` (starting / success / failure) |
| Thêm / refresh channel | `add_channel`, `channel.refresh_config` (success / failure) |
| Set / remove connector | `connector.set.*`, `connector.remove.*` (success / failure) |
| OAuth refresh | vòng lặp refresh — chỉ báo khi đổi trạng thái ok↔fail theo từng provider |
| Cài skills | `skills.install` (success / failure) |
| Soft reset thiết bị | `device.soft_reset` |
| Claude Code login / WhatsApp pair | kết quả pairing cuối (paired / failure / timeout) |
| Đổi default model | model sync — chỉ khi primary/image model (đã gate theo version) thực sự đổi |

Chuyển runtime là thao tác độc quyền. Trong khi một lượt cài đặt hoặc chuyển
backend đang chạy, `POST /api/device/agent-runtime` tiếp theo nhận `409 Conflict`
thay vì khởi động một transition systemd cạnh tranh. Selector trên web bị khoá
cho đến khi lượt đầu được xác nhận hoặc timeout.

Switch được kích hoạt qua HTTP còn yêu cầu xác nhận runtime đã sẵn sàng trong
tối đa 60 giây trước khi dừng runtime cũ và lưu `agent_runtime`; chỉ
`systemctl is-active` không bao giờ được coi là bằng chứng gateway đã phục vụ
request được. Mỗi runtime có probe riêng: OpenClaw chạy RPC status đã xác thực,
Hermes poll `/health` đã xác thực, còn PicoClaw, Codex, Claude Code và OpenCode
phải chấp nhận WebSocket upgrade đã xác thực. MQTT runtime setup dùng chính các
probe này: publish `starting` ngay, chỉ publish `success` sau khi target pass
probe (hoặc `failure` sau rollback). Ack success được gửi trước os-server restart
bắt buộc để chắc chắn đến được broker.

Khi boot sau một runtime switch, startup sequence vẫn có thể reconcile config,
channel và file onboarding của runtime; các bước này có thể restart gateway.
Trước khi gửi wake greeting vật lý, os-server vì vậy yêu cầu gateway active giữ
trạng thái ready liên tục trong 15 giây. Điều này tránh gửi greeting vào gateway
đã pass một health probe cũ nhưng vẫn đang restart. System greeting cũng báo cho
agent biết các skill của thiết bị đã sẵn sàng; agent chỉ dùng skill phù hợp cho
yêu cầu hành động hoặc liên quan đến thiết bị ở lượt sau, thay vì quét mọi skill
khi boot. Greeting cũng có context có cấu trúc `agent_runtime` lấy từ display
name của gateway đã ready (ví dụ `OpenClaw` hoặc `Codex`), để agent theo đúng
workspace instruction về tool và session convention của runtime đó. Nó cũng có
`device_type` đã resolve cùng danh sách `device_capabilities` đã sort từ
`ROBOT.md` của device, để agent không giả định phần cứng không tồn tại. Nguồn
runtime này cố ý là gateway đã ready, không phải `config.agent_runtime`, vì
config có thể lệch tạm thời trong khi reconcile runtime switch.

Cảnh báo bật khi `llm_base_url` + `llm_api_key` được set; đặt
`alerts_disabled: true` trong `config/config.json` để tắt cảnh báo cho một thiết bị.

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
| POST | `/servo/track` | Bắt đầu tracking — `{"target":"cup"}` (tự detect) hoặc `{"bbox":[x,y,w,h]}`. Xem [vision-tracking_vi.md](../../robots/lamp/docs/vi/vision-tracking_vi.md) |
| POST | `/servo/track/stop` | Dừng phiên tracking |
| GET | `/servo/track` | Trạng thái tracking (active, target, bbox, confidence) |
| POST | `/servo/track/update` | Khởi tạo lại tracker với bbox mới |

### LED (64 WS2812, grid 8x5)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/led` | LED strip info |
| GET | `/led/color` | Màu LED hiện tại |
| POST | `/led/solid` | Fill toàn bộ 1 màu |
| POST | `/led/paint` | Set từng pixel (array tối đa 64), hoặc gradient stops với `"gradient": true` |
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

> Wellbeing activity history giờ nằm trên OS server HTTP API (port 5000). Xem `POST /api/wellbeing/log` và `GET /api/agent/wellbeing-history` — entries ghi JSONL tại `/root/local/users/{user}/wellbeing/YYYY-MM-DD.jsonl` với schema `{ts, seq, hour, action, notes}` (action ∈ `drink`/`break`/`sedentary`/`emotional`). HAL không còn host endpoint wellbeing.

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

### Piper — TTS chạy trên thiết bị

Provider TTS thứ ba bên cạnh `openai` và `elevenlabs`, chọn bằng
`tts_provider: "piper"`. Tổng hợp giọng chạy ngay trên máy, gỡ được hai giới
hạn mà nhà cung cấp đám mây áp đặt: không còn hạn mức đồng thời dùng chung để
phải xếp hàng (mỗi máy tự dựng tiếng của mình, nên năng lực tăng theo số máy
bán ra và không tốn phí mỗi câu), và không còn vòng mạng, nên thời gian tới âm
thanh đầu tiên giảm mạnh — đo được 129–236 ms với câu ngắn, so với 2–5 s của
một lượt gọi đám mây. Đánh đổi là chất lượng: Piper nghe rõ ràng kém hơn giọng
neural đám mây, nên nó đóng vai giọng mặc định miễn phí chứ không phải bản thay
thế.

**Không có gì nằm trong image.** Engine (~26 MB) và mỗi giọng (~63 MB) chỉ được
tải về khi người dùng yêu cầu trong Settings → Voice. Nhờ vậy image không phình,
máy nào không dùng thì không tốn gì — và vì chính thiết bị của người dùng tải từ
nguồn gốc, Autonomous không rơi vào vai bên phân phối lại phần mềm GPL-3.0.
Nhét Piper vào image sẽ đảo ngược điều đó; xem `CREDITS.md`.

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/voice/piper/status` | Engine đã cài chưa, giọng nào đã có, danh mục tải được, và job đang chạy nếu có. Proxy sang HAL rồi bọc lại theo envelope chuẩn — client web từ chối payload trần. |
| POST | `/api/voice/piper/install` | Cài engine. Idempotent: đã cài rồi thì trả ok, nên UI gọi thẳng không cần kiểm tra trước. |
| POST | `/api/voice/piper/voice` | Tải một giọng trong danh mục. Body `{name}`; tên ngoài danh mục bị từ chối, nên không ai biến endpoint này thành đường tải file tuỳ ý vào `/opt/piper`. |
| POST | `/api/voice/piper/voice/remove` | Xoá một giọng đã tải, lấy lại ~63 MB. Body `{name}`, cũng chỉ nhận tên trong danh mục vì cùng lý do — tên tuỳ ý ở đây là xoá file tuỳ ý. Từ chối xoá giọng cuối cùng còn lại. |

Cả bốn đều admin-gated: chúng cài phần mềm và ghi 63–79 MB mỗi giọng. HAL phục
vụ đúng bốn endpoint đó dưới `/voice/piper/*`; việc tải chạy nền và báo

`piperProxy` **thử lại POST trong lúc HAL chưa trả lời**, tối đa 25 giây. Mỗi lần
lưu voice là HAL restart (~8 giây chết, đôi khi gấp đôi vì hai đường config đều
xin restart), và một cú Download hay Remove rơi đúng cửa sổ đó là mất trắng —
trang báo không có gì thay đổi còn người dùng phải tự đoán khi nào thử lại.
Chỉ **dial thất bại** mới được thử lại, và chính chỗ phân biệt này gánh toàn bộ
lập luận an toàn: dial không kết nối được là bằng chứng request chưa hề được
gửi đi, nên gửi lại không thể lặp lại tác dụng nào. Còn timeout thì không chứng
minh được điều đó — deadline bao cả lúc đọc phản hồi, nên HAL hoàn toàn có thể
đã làm xong việc rồi trả lời chậm — nên timeout bị trả về như một lỗi thường.
Mọi phản hồi, kể cả một lời từ chối, đều là chung cuộc và được chuyển thẳng về.
GET cố ý không thử lại —
chính việc status poll hỏng mới là thứ báo cho trang biết máy đang khởi động
lại, giữ chúng mở chỉ làm dồn request và giấu mất trạng thái. Có test trong
`piper_test.go`, dựng lại listener ngay dưới lời gọi.

**Lượt tải không chạy bên trong HAL.** `hal/routes/piper_download.py` được
`systemd-run` khởi động thành một transient unit, và hai bên thống nhất với
nhau qua file job `/var/lib/autonomous/piper-job.json` thay vì bộ nhớ chung.
Đây không phải vẽ vời: lưu **bất kỳ** thiết lập voice nào cũng khiến os-server
gọi `systemctl restart hal` (`device/config_update.go`), mà hal.service để
`KillMode=control-group`, nên một luồng trong process — hay bất kỳ tiến trình
con thường nào — đều bị giết giữa lúc đang tải. Bản ghi job chết theo, nên
trang quay về `Download 63 MB` như thể chưa hề bấm gì, không lỗi, không có gì
để thử lại. Worker không import bất cứ thứ gì từ `hal`: package đó kéo theo
driver phần cứng ngay khi import, thứ mà một trình tải file không có việc gì
phải đụng vào, và việc không phụ thuộc gì cũng giúp nó chạy tiếp cả khi HAL
không khởi động nổi.

Mỗi lượt chạy có **tên unit riêng** (`autonomous-piper-download-<ns>`). Tên cố
định sẽ đụng lượt trước đó: một unit vừa xong còn nằm ở `inactive` một lúc trước
khi `--collect` dọn, mà `systemd-run` thì từ chối cái tên vẫn còn tồn tại. Cú
thất bại đó rơi xuống nhánh dự phòng chạy trong process HAL, rồi chết theo lần
restart kế tiếp và hiện ra thành *download stopped unexpectedly* mà không rõ lý
do. Nhánh dự phòng giờ log lại đúng stderr của systemd, vì rơi xuống nó trong im
lặng chính là cách một lượt tải chui vào control group của HAL mà không ai hay.

Không có gì restart HAL vì một lượt tải. Danh sách giọng đọc từ filesystem theo
từng request, đường dẫn model phân giải theo từng câu nói, nên giọng vừa có file
là liệt kê và nói được ngay — đã đo: tải xong lúc 18:32:29 trên một HAL khởi
động lúc 18:31:59, tới 18:33:11 liệt kê và nói được mà không restart lần nào.
Việc apply một giọng cũng **không** còn restart HAL. `POST /voice/tts/config`
đặt provider, voice, key và base URL thẳng vào TTS service đang chạy, mà service
đọc cả bốn thứ đó theo từng câu nói, nên thay đổi ăn ngay từ câu kế tiếp.

Những câu máy nói về chính nó — restart, shutdown, reboot, sleep — được
**dựng sẵn vào cache TTS**, lúc boot và mỗi khi `/voice/tts/config` đổi provider
hoặc giọng (cache key gồm cả hai, nên đổi giọng là mất sạch clip cũ). Chúng phát
đúng vào những lúc tệ nhất: câu báo restart nói trong lúc HAL đang tắt, câu chào
boot nói lúc mọi service khác còn đang lên. Với Piper, cache miss ở đó nghĩa là
nạp model 63 MB trên một CPU đang nghẹt — đo trên sun60iw2 8 nhân, riêng phần
nạp đã 2–3,4 giây và câu báo restart tổng hợp ở mức 1,1x realtime, sát ngưỡng
tới mức chỉ cần tải nặng thêm chút là luồng audio đói dữ liệu và giọng nghe
nhão. Còn cache hit thì không tốn tổng hợp gì cả.

Cờ realtime giờ so sánh trước/sau chứ không phản ứng theo việc "có gửi kèm".
Trang settings nhét khối `realtime` vào **mọi** lần lưu, nên coi nó là thay đổi
thì lần lưu nào cũng restart HAL — và việc đẩy TTS live ở trên sẽ thành code
chết.

### Bộ mặc định Autonomous

Máy xuất xưởng mang credential proxy của team Autonomous trong `llm_api_key`,
`llm_model` và `llm_base_url`, và mọi mục khác đều khởi đi từ đúng ba giá trị
đó. Gõ key cá nhân đè lên là xoá sổ chúng — đã có máy ra tới tay người dùng mà
không còn đường nào quay lại bộ credential nó được bán kèm.

`autonomous_defaults` là một object ở **cấp ngoài cùng** của `config.json`, giữ
`base_url` / `api_key` / `model`. Nó được ghi **đúng một lần**, bởi
`captureAutonomousDefaults`, ngay trước lần lưu đầu tiên có mang theo bất kỳ
credential nào — LLM, TTS, STT hay key/URL của realtime — và không bao giờ ghi
lại. Chụp lần hai là lưu chính key của người dùng dưới tên Autonomous và mất
hẳn bộ thật, đúng cái hỏng mà nó sinh ra để chặn. Lần lưu không đụng credential
nào (wifi, đổi tên, channel) thì không kích hoạt, và config không có gì để giữ
thì bỏ qua, để một bộ rỗng không bị nhầm là mặc định hợp lệ. Chỉ factory reset
mới xoá nó.

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/device/restore-defaults` | Đưa một mục về lại credential xuất xưởng. Body `{"section": "llm" \| "voice" \| "realtime"}`. Admin-gated. |

Khôi phục theo **từng mục**, vì người dùng nghĩ theo cách đó — họ đổi brain, hoặc
đổi nhà cung cấp giọng, và muốn lấy lại đúng thứ đó. Mỗi mục lấy phần của bộ đã
lưu mà nó vốn khởi đi: AI Brain lấy url + key + model, realtime và voice lấy
url + key. Riêng qwen realtime bị từ chối: nó nói thẳng với host Alibaba bằng
credential riêng, đưa bộ xuất xưởng vào đó chỉ tổ nhận 401.

Nó được cài đặt như một lượt `UpdateConfig` bình thường chứ không ghi thẳng, nên
thừa hưởng đủ mọi side-effect của một lần sửa tay — restart hal hoặc đẩy TTS
live, sync model sang gateway, reset phiên agent. Tự viết một hàm lưu riêng sẽ
lệch khỏi danh sách đó ngay lần đầu có người thêm việc vào.

`has_autonomous_defaults` trong `GET /api/device/config` chỉ nói có hay không,
không bao giờ trả giá trị. Web dùng nó để quyết định có hiện nút hay không.

HAL đọc **thông tin đăng nhập của riêng từng dịch vụ**, thiếu thì mới lùi về
của AI Brain: `tts_api_key`/`tts_base_url` cho TTS, `stt_api_key`/`stt_base_url`
cho STT, còn lại mới dùng `llm_api_key`/`llm_base_url`. Trên đa số máy cả ba là
cùng một chuỗi, vì trang settings tự mirror key và URL của brain sang hai chỗ
kia khi chúng còn trống. Nó chỉ lộ ra khi brain trỏ đi nơi khác: một máy có
`llm_base_url` ở openrouter và `tts_base_url` ở proxy autonomous đã ghép thành
`openrouter.ai/api/v1/elevenlabs/text-to-speech/…` và ăn 404 ở mọi câu nói, vì
backend ElevenLabs nối thêm `/elevenlabs` vào bất kỳ base nào được đưa — mà nó
được đưa base của brain. Config vốn có URL đúng từ đầu; chỉ là không ai đọc.

`device/config_update.go` tách cái `voiceSnapshot` cũ làm hai: `bootSnapshot`
(key và URL của LLM, STT — HAL đọc thật lúc import, vẫn đáng restart) và
`ttsSnapshot` (provider, voice, key và URL của TTS — đẩy thẳng vào lúc chạy).
Đổi giọng là thao tác lưu thường gặp nhất, mà restart vì nó thì micro, loa và
wake word chết theo mười tới mười lăm giây; mọi cú bấm rơi vào cửa sổ đó đều
mất, vì HAL không nghe. Nếu đẩy live thất bại, os-server quay về restart — một
giọng đã lưu mà không bao giờ tới được HAL còn tệ hơn cái restart nó tránh.

Job được **đánh dấu đang chạy trước khi POST trả lời**, và phản hồi mang theo
job đó. Để worker tự đánh dấu là thua một cuộc đua mà UI không gỡ lại được:
panel chỉ poll *trong lúc* job đang chạy, nên nếu lần đọc đầu tiên rơi vào
trước lần ghi đầu tiên của worker, nó kết luận không có gì bắt đầu rồi thôi
không hỏi nữa — và một lượt tải dài vài phút chạy xong trong vô hình. Đánh dấu
trong cùng cái lock đang kiểm tra job cũng làm hai cú double-click chỉ thành
một lượt tải.

Bên đọc chỉ tin một job là đang chạy khi pid của nó còn sống, nên worker bị
giết bởi thứ gì khác ngoài chính error handler của nó sẽ hiện là đã dừng, chứ
không phải một lượt tải đứng hình vĩnh viễn. Việc quét rác lúc khởi động cũng
chừa ra file của job đang chạy — lượt tải giờ sống lâu hơn HAL, nên lần quét đó
chạy *trong lúc* đang tải, và xoá file `.part` của nó là phá đúng cái tình
huống mà thiết kế này sinh ra để bảo vệ.

Job báo thêm `bytes_done`/`bytes_total` bên cạnh `percent`, chỉ đếm cho model —
file sidecar vài KB sẽ làm bộ đếm nhảy về một tổng bé xíu rồi quay lại. Lượt
tải giọng thất bại tự xoá file dở của nó, và HAL quét dọn sidecar mồ côi cùng
file `.part` một lần lúc khởi động: danh sách giọng đọc theo `.onnx`, nên một
sidecar mà model không bao giờ về là thứ vô hình trên UI nhưng vẫn chiếm chỗ
thật trên thẻ nhớ.

Việc xoá giữ đúng một bất biến: **không bao giờ xoá model cuối cùng.** HAL không
được cho biết giọng nào đang cấu hình — os-server gửi kèm trong từng lượt
`/voice/speak` — nên nó không thể từ chối "cái đang dùng", và nó không giả vờ
làm được. Xoá giọng khác thì sống sót được, vì giọng không tìm thấy sẽ lùi về
một giọng đã cài; xoá cái cuối cùng thì không, vì backend hết thứ để nạp và máy
câm luôn. UI thì ẩn nút Remove ở dòng đang dùng, nên đổi giọng phải xảy ra
trước khi xoá.
tiến độ qua trường `job` trong status, vì kéo 63 MB lâu hơn nhiều so với thời
gian nên giữ một HTTP request mở.

Hai chỗ dễ làm sai nếu chép lại cẩu thả. Đầu ra của Piper vốn đã đạt biên độ tối
đa, nên `volume_boost` 2.5 mà các backend đám mây dùng sẽ **clip nát mọi nguyên
âm** — backend này khai `1.0`. Và nạp model tốn ~700 ms, đủ để chi phối thời gian
tới âm thanh đầu tiên với câu ngắn, cho tới khi backend giữ sẵn một tiến trình
đã nạp model và sinh cái thay thế sau mỗi lượt nói.

Danh sách giọng đọc từ filesystem (`/opt/piper/voices/*.onnx`) chứ không phải từ
danh sách cứng, nên thả một model vào là chọn được ngay. Còn model nào được
**mời tải** lại là quyết định về license, ghi kèm từng mục trong
`hal/drivers/voice/tts/piper_catalog.py`.

Backend báo là *sẵn sàng* khi có binary và **bất kỳ** giọng nào, chứ không phải
đúng giọng đang cấu hình. Máy hoàn toàn có thể đang trỏ tới một giọng chưa có —
người dùng lưu lựa chọn trong lúc model 63 MB còn đang tải — và nếu chặn theo
đúng tên thì cả khối TTS tắt luôn. Thay vào đó, giọng không tìm thấy sẽ lùi về
giọng mặc định, rồi lùi tiếp về bất kỳ giọng nào đã cài, và ghi log một lần cho
mỗi tên. Nói sai giọng là lỗi tự nó giải thích được; máy im tiếng thì người dùng
hiểu là hỏng phần cứng.

`GET /api/device/voices?provider=piper` **báo lỗi chứ không trả mảng rỗng** khi
không gọi được HAL. Giọng là file nằm dưới `/opt/piper`, nên HAL là thứ duy nhất
biết máy đang có gì; trả về rỗng là nói một điều os-server không có cơ sở để
nói, mà web thì coi câu trả lời đó là chính thức — dropdown rỗng đi, và vì nó
chỉ fetch lại khi đổi provider hoặc ngôn ngữ nên rỗng luôn không bao giờ đầy
lại. Mỗi lần lưu voice là HAL restart, nên cửa sổ đó bị rơi vào thường xuyên.
Trả lỗi thì client giữ nguyên danh sách tốt cuối cùng.

Cũng vì vậy `domain.TTSVoicesByProvider` để **rỗng** cho Piper: không image nào
kèm sẵn giọng, nên mọi cái tên đặt ở đó làm fallback đều là tên máy không có —
và web sẽ lưu đúng cái tên đó thành giọng đang dùng.


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
   - Seed `device_type` từ device class đã resolve (env `DEVICE_TYPE`, không có thì lấy key sẵn có) để config.json mang giá trị này cho các bên đọc không có env — wake word của HAL và `software-update`. Provisioning chỉ ghi env, nên không có seed này thì key không bao giờ tồn tại trên máy đã provision. Chỉ ghi khi giá trị đang lưu khác giá trị resolve
   - Seed `tts_provider` + `tts_voice` từ block `voice:` trong ROBOT.md khi user chưa chọn (ghi một lần; lựa chọn đã lưu của user luôn thắng; provider vắng/không hợp lệ → `openai`). Khi provider seed là `elevenlabs` mà không khai báo voice, chọn default theo ngôn ngữ (`vi`→Ngan, `zh`→Amy, còn lại Rachel)
3. Nếu `SetUpCompleted`:
   - Kết nối OpenClaw WebSocket
   - Kết nối MQTT
   - Start ambient behaviors
   - Chờ HAL trả lời `GET :5001/health` (tối đa 120s) trước mọi lời gọi HAL. os-server bind :5000 sớm hơn hẳn lúc FastAPI của HAL lắng nghe, lần boot đầu còn phải dựng venv và load model, nên một lời gọi một-lần không có hàng rào sẽ mất trắng vì connection refused
   - Đặt volume loa: mức user chỉnh gần nhất (HAL ghi lại mỗi lần `/audio/volume`) được ưu tiên; không có thì lấy `startup_volume` của thiết bị (front matter ROBOT.md, mặc định 100)
4. Nếu chưa setup: chờ `POST /api/device/setup`

## Logging

`HAL_LOG_LEVEL` trong `/opt/hal/.env` dùng chung điều khiển mức log cho HAL,
OS Server và bootstrap. Các giá trị hợp lệ là `DEBUG`, `INFO` (mặc định),
`WARN`, và `ERROR`. OS Server ghi các record từ mức đã cấu hình trở lên ra stdout
và file cục bộ xoay vòng `/var/log/os-server.log` (mỗi file 2 MB, giữ lại 10
bản sao mới nhất).

Khi có cấu hình `GELF_URL`, OS Server gửi các record từ cùng mức đã cấu hình trở lên
tới collector tập trung bằng một worker với queue giới hạn 256 record. Logging không
block request path và không tạo goroutine theo từng record: khi collector chậm/không
hoạt động và queue đầy, GELF record mới bị drop (có stderr notice rate-limit); log
console và rotating file cục bộ vẫn tiếp tục. Khi shutdown, worker flush record trong
queue tối đa năm giây trước khi hủy delivery còn lại.

## Local Intent Matching

Khi nhận event `voice_command`, `voice_followup` hoặc `voice`, OS server check local intent trước (~50ms):

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
| "tăng âm", "volume up" | volume 100 |
| "giảm âm", "volume down" | volume 30 |
| "mute speaker" | `POST /speaker/mute` (im lặng — không TTS xác nhận) |
| "unmute speaker" | `POST /speaker/unmute` + "Speaker on!" |

Keyword match theo nguyên cụm với word boundary ASCII — "unmute speaker" không kích rule "mute speaker". Các rule chitchat (chào / tạm biệt / cảm ơn, match theo từng ngôn ngữ) dùng chung phép kiểm tra boundary đó: trước đây match chuỗi con thô khiến phrase 2 ký tự "hi" khớp nằm trong "this", "his", "machine", nên câu bình thường như "What is this?" bị trả lời tại chỗ bằng "Hi there!" và không bao giờ tới agent.

Chitchat **tắt khi realtime voice agent đang bật** — model nhận mọi lượt voice trước os-server và tự trả lời phần xã giao, đúng nhân cách của nó. Bật cả hai nghĩa là một câu canned với giọng khác chen ngang đúng những lượt model tình cờ im. Các rule lệnh phía trên vẫn chạy trong mọi trường hợp vì chúng thật sự nhanh hơn một vòng model. Cổng này bám theo `realtime.enabled` ngay lúc chạy, đổi trong Settings không cần restart.

Không match → forward OpenClaw.
