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
  "images": ["<base64 JPEG>", "…"]   // tùy chọn, mỗi ảnh đính kèm một phần tử
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
  "images": ["<base64 JPEG>", "…"]   // tùy chọn, mỗi ảnh đính kèm một phần tử
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
4. Nếu event có `images` → gọi `SendChatMessageWithImages` → gửi mọi ảnh đính kèm cùng text cho AI vision phân tích. Là một DANH SÁCH chứ không phải một trường đơn: client chat có thể đính nhiều ảnh cùng lúc và mọi wire format phía sau gateway vốn đã mang `attachments[]`; event camera thì chỉ gửi một phần tử. Với type chat (`web_chat` / `mqtt_chat`), mỗi ảnh được lưu vào `/tmp/web-chat-<ms>-<i>.jpg` (có index nên các ảnh trong CÙNG một lượt không đè tên nhau) và gắn tag `[image: <path>]` để agent reference (vd: face enrollment). Khi model chính không đọc được ảnh, describe-first gate chạy một lần CHO MỖI ảnh, **song song** (`safego`), và mô tả được đánh số `(image N of M)`. Song song ở đây không phải để tối ưu: gate chạy ngay trong HTTP handler nên POST của client không trả về cho tới khi describe xong hết — một lần describe đo được 8-38 giây, nên 2 ảnh chạy tuần tự làm web chat im lặng ~53 giây, đủ lâu để người dùng reload trang (mà reload thì huỷ request và mất luôn lượt đó). Chạy song song biến thời gian chờ thành ảnh CHẬM NHẤT thay vì tổng của chúng.
5. Describe-first gate ở trên CHỈ phủ ảnh đi vào lượt từ BÊN NGOÀI (đính kèm chat/Telegram, look-frame do realtime voice bàn giao). Ảnh agent tự chụp GIỮA LƯỢT bằng `/camera/snapshot` không đi qua gate đó — tool shell chỉ trả về `{"path": ...}`, model chính text-only không nhìn thấy gì. Cho đường này, skill `camera` gọi `POST /api/vision/look` (loopback-only, `system/server/vision.go`) thay vì gọi thẳng HAL: os-server tự chụp (`hal.Snapshot`, 768px/q75 chốt ở server) rồi trả `{"path": ..., "description": ...}`. Nhánh quyết định model có nhìn được ảnh hay không nằm Ở ĐÂY chứ không nằm trong skill — khi `vision.ModelSupportsVision` báo model chính tự đọc được ảnh thì BỎ QUA describe hoàn toàn (không gọi vision model, không mất 8-38 giây), chỉ trả `path` để agent tự mở. Describe lỗi thì trả 502 để agent nói thẳng là không nhìn được thay vì đoán bừa
6. Run chat (`web_chat` / `mqtt_chat`) được mark qua `MarkWebChatRun(runID)` để SSE handler suppress TTS lúc lifecycle end — reply chỉ hiện trong UI chat (web SSE, hoặc stream MQTT `chat.event`).

### OpenClaw

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| GET | `/api/agent/status` | Trạng thái kết nối WS; gồm `uptime` (uptime WS phía OS server) và `agentUptime` (uptime tiến trình OpenClaw, không reset khi OS server restart) |
| GET | `/api/agent/events` | SSE stream events real-time |
| GET | `/api/agent/recent` | 100 events gần nhất (ring buffer) |
| POST | `/api/agent/speech/cancel` | Cử chỉ huỷ vật lý (single click, do HAL gọi — auth loopback-only để nút vẫn chạy khi chưa login). Bịt miệng mọi turn đang chạy và dừng playback ở HAL (`StopTTS`, đồng thời xoá luôn hàng đợi speak đã pre-synth). **Không** abort turn: turn vẫn chạy tiếp, tool vẫn fire, text vẫn về web chat và history — chỉ mất quyền dùng loa. Cài đặt bằng một watermark unix-ms đơn điệu (`speechWatermarkMs`): `deliverTTS` bỏ mọi câu trả lời thuộc turn được tạo tại hoặc trước mốc, kèm flow event `tts_cancelled`. Tuổi của turn đọc từ runID — id thiết bị kết thúc bằng timestamp tạo (`device-chat-7-<unix-ms>`, 13 chữ số), id kênh (`tg-<messageID>`) không có nên fallback về thời điểm đầu tiên run đó xin nói. Vì turn mới luôn nằm phía sau mốc, user click xong nói ngay được trong khi backlog cũ chạy nốt trong im lặng; watermark không bao giờ cần xoá. Cùng cái mốc đó cũng chặn luôn marker `[HW:]` của turn tại `fireHWCall` — servo và LED dừng theo, vì thiết bị vẫn cựa quậy sau khi bị bảo dừng thì user đọc là "nó phớt lờ mình". runID được đưa qua `resolveRunID` trước: đường TTS đã cầm id thiết bị trong khi đường HW có thể còn cầm UUID gốc của backend cho CÙNG một turn, và phán riêng lẻ thì câu trả lời bị bịt trong khi marker vẫn fire. Riêng `/dm`, `/broadcast`, `/speak` được miễn (cổng chặn đặt sau chúng): click nghĩa là "đừng nói với tôi", không được nuốt câu trả lời gửi cho user Telegram. Một watermark **thứ hai** (`autoSpeechWatermarkMs`) hoạt động y hệt nhưng do hệ thống đóng mốc: nó tiến lên mỗi khi HAL báo `voice_agent_handled` — realtime voice agent vừa trả lời thành tiếng một câu MỚI hơn — nên turn agent chính còn đang xử lý câu trước đó mất loa thay vì trả lời muộn bằng một giọng khác. `deliverTTS` bỏ câu trả lời cũ hơn **bất kỳ** mốc nào trong hai; `fireHWCall` **chỉ** xét mốc của cú click, vì phán đoán do máy đưa ra không được phép âm thầm huỷ hành động user đã yêu cầu. Opt-in theo từng body: đặt `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1` trong `/opt/hal/.env` của body. Mặc định TẮT, nên body chưa từng biết tới switch này không bị ảnh hưởng. Cú click cũng gọi `FillerManager.CancelAllActive()`. Filler nói thẳng xuống HAL, không đi qua `deliverTTS`, nên watermark một mình không với tới được — mà turn bị bịt tiếng thì vẫn chạy tiếp, nên mỗi lần nó xong một tool là lại re-arm thêm một câu "một giây nhé" cho một câu trả lời user vừa huỷ. Mọi run đang giữ trạng thái filler tại thời điểm đó đều nằm phía cũ của mốc nên bị bỏ hết; filler Opening của câu user nói TIẾP THEO được arm sau đó nên không bị ảnh hưởng. Câu trả lời bị bỏ vẫn được POST sang `POST /voice/realtime/history` của HAL: cú click lấy đi cái loa chứ không lấy đi câu trả lời, mà bản ghi của realtime về những gì agent chính đã đáp vốn treo ở lúc TTS phát xong (xem `docs/realtime-voice.md`). |
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
| GET | `/servo` | Recordings + animation state + `motion_mode` (`zero` / `hold` / `released`, hoặc `null` khi không mode nào đang giữ body) — chế độ tư thế quyết định `/servo/play` có được thực thi hay không |
| POST | `/servo/play` | Phát animation (idle, curious, nod, headshake, happy_wiggle, sad, excited, shock, shy, scanning, wake_up, music_groove, listening, thinking_deep, laugh, confused, sleepy, greeting, acknowledge, stretching). Idle tự chạy khi boot. Trả `{"status":"ignored","reason":"hold"\|"zero"\|"released"\|"sleeping"}` khi mode hoặc sleep gate bỏ qua lệnh — `"ok"` nghĩa là recording đã thực sự chạy. |
| POST | `/servo/move` | Gửi joint positions với smooth interpolation |
| POST | `/servo/release` | Tắt torque tất cả servo |
| GET | `/servo/position` | Vị trí servo hiện tại |
| GET | `/servo/aim` | Danh sách aim directions |
| POST | `/servo/aim` | Aim đầu thiết bị (center, desk, wall, left, right, up, down, user). `left`/`right` chỉ đổi `base_yaw`; `center` gọi tường minh thì reset nó; các hướng còn lại — và fallback khi hướng lạ — giữ nguyên yaw hiện tại |
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

## Chạy off-device (laptop)

`make os-dev` chạy **đúng binary** được ship lên board — không build tag, không
có nhánh code thứ hai. Chỉ các đường dẫn tuyệt đối của thiết bị là thay đổi,
qua các biến môi trường mà `system/lib/syspath` đọc. **Không set env = mặc định
của board, giống từng byte** (`runtimes/codex/paths_default_test.go` kiểm chứng
điều này).

| Biến môi trường | Mặc định (device) | Dùng cho |
|-----------------|-------------------|----------|
| `CODEX_HOME` | `/root/.codex` | State dir của Codex — config.toml, auth.json, `.env`, `skills/`, `sessions/`, `workspace/`. Là gốc của mọi đường dẫn codex ở cả client lẫn `codex-gatewayd` |
| `CODEX_PORT` | `18792` | Cổng WebSocket của bridge (`WSURL` và listener của gatewayd) |
| `CODEX_WS_TOKEN` | `autonomous_codex_token` | Bearer token os-server gửi tới bridge |
| `OS_AGENT_HOME` | `/root` | Gốc để một coding session Telegram resolve `~` và đường dẫn tương đối |
| `OS_AGENT_STATE_PATH` | `/root/config/agent_state.json` | Lịch sử chuyển runtime (persona migration) |
| `OS_BOOTSTRAP_CONFIG` | `/root/config/bootstrap.json` | File os-server đọc `metadata_url` — base cho skill zip và skill watcher |
| `OS_LOG_FILE` | `/var/log/os-server.log` | File log xoay vòng |
| `DEVICE_TYPE` / `DEVICES_DIR` | — / `/opt/devices` | Chọn body và gốc `robots/<type>/` (đã có sẵn) |

`config.json` không cần env: `configPath` là `config/config.json` tương đối theo
cwd, nên `os-dev` chạy từ state dir đúng như `WorkingDirectory=/root` của systemd
trên board.

Một stack đầy đủ trên laptop cần ba terminal:

```bash
make sim          # HAL trên :5001
make codex-dev    # codex bridge trên $CODEX_PORT
make os-dev       # API trên :5000
make web-dev      # web UI trên :5173 (tuỳ chọn)
```

os-server không serve HTML: trên board là nginx serve `web/dist` rồi proxy `/api`
và `/hw` xuống nó. `make web-dev` đặt Vite vào đúng vai nginx, với `LAMP_PROXY`
(mặc định `http://127.0.0.1:5000`) là thiết bị mà SPA nói chuyện cùng — file
`.env` trong `web/` vẫn thắng, nên trỏ vào Pi thật thì không đổi gì. Mở
**`http://localhost:5173/monitor`**; Vite chỉ bind `[::1]` nên `127.0.0.1:5173`
bị từ chối. Các route admin cần auth — đăng nhập bằng mật khẩu thiết bị, hoặc
thêm `?llm_api_key=<key trong config.json>` một lần, SPA sẽ đổi nó lấy session
cookie rồi xoá khỏi thanh địa chỉ.

Ba trong sáu tab log chạy được off-device. `hal` và `os-server` đi theo
`OS_HAL_LOG_FILE` / `OS_LOG_FILE`, còn các tab Agent đi theo
`OS_AGENT_BRIDGE_LOG` — `make codex-dev` tee bridge ra file vì laptop không có
journal để đọc. `bootstrap` (worker không chạy off-device) và `buddy` (app Mac,
không có log ở đây) để trống có chủ đích; env unset thì cả sáu vẫn resolve đúng
như trên board.

Các núm trong Makefile: `OS_STATE_DIR` (mặc định `/tmp/autonomous-os`),
`OS_AGENT_RUNTIME` (mặc định `codex`), `CODEX_HOME` (mặc định `$HOME/.codex`),
`CODEX_PORT`, `CODEX_BIN`. `scripts/dev/os-dev-seed.sh` ghi `device_type`,
`agent_runtime` và `set_up_completed: true` vào config.json của state dir — cái
cuối quan trọng vì startup sequence chạy presync và `EnsureOnboarding` bị gate
bởi nó (`server/config_watch.go`), thiếu nó thì workspace sẽ rỗng. Target không
tự cài codex CLI — nó được xem như đã có sẵn trên `PATH`.

Nhưng skills thì tự cài. `os-dev-seed.sh` còn seed một `bootstrap.json` chứa
`metadata_url`, dựng từ chính `GCS_BUCKET` / `BUCKET_PREFIX` khai trong
`scripts/release/ota-config.sh`, nên URL dev không thể lệch với thứ
`upload-skills.sh` publish. Có nó rồi thì `EnsureOnboarding` chạy đúng
`downloadSkills()` như trên board: mọi skill mà `DEVICE_TYPE` này hỗ trợ được tải
về dạng `<base>/skills/<name>.zip` vào `$CODEX_HOME/skills`, sau đó skill watcher
tự cập nhật khi version đổi. Object trên CDN là public nên không cần credential.
Seed một lần — `bootstrap.json` đã sửa sẽ được giữ nguyên.

`metadata_url` là key DUY NHẤT os-server đọc từ file đó, và chỉ có skill watcher
cùng helper `otaBaseURL()` của các runtime dùng tới, nên bật nó off-device chỉ
mở đúng phần skills — OTA tự cập nhật nằm ở binary `bootstrap-server` riêng, mà
`make os-dev` không chạy.

### Đầy đủ media + giọng nói trên laptop

`make sim` không thôi thì HAL boot với thiết bị ảo. `make sim SIM_MEDIA=host` mở
microphone, speaker và camera của Mac **và** chạy pipeline giọng nói thật (STT →
realtime → dispatch `[turn] route=…` → server này), nên một lượt nói đi đúng
đường mà nó đi trên board. Target `sim` set sẵn ba đường dẫn cho việc đó:

| Env | Trỏ tới | Vì sao |
|-----|---------|--------|
| `OS_CONFIG_PATH` | `$OS_STATE_DIR/config/config.json` | File duy nhất HAL và os-server dùng chung, đúng vai `/root/config/config.json` trên board. Mang credential **và** `agent_runtime` |
| `HAL_SNAPSHOT_DIR` | `$CODEX_HOME/media/hal-snapshots` | Nơi `?save=true` ghi file. Bắt buộc nằm dưới home của chính runtime, nếu không agent không đọc lại được frame và `GET /api/sensing/agent-snapshot/…` không serve được |
| `HAL_SNAPSHOT_PERSIST_DIR` | `$SIM_STATE_DIR/snapshots` | `/var/lib/hal/snapshots` chỉ root ghi được |
| `HAL_TTS_CACHE_DIR`, `HAL_CALIBRATION_DIR`, `HAL_USER_BEARING_PATH`, `HAL_FACE_HEIGHT_PATH`, `HAL_VOICE_STRANGERS_DIR`, `HAL_DL_STALL_LOG` | `$SIM_STATE_DIR/…` | Phần state ghi được còn lại của HAL, trên board nằm ở `/var/lib/hal` hoặc `/root/local` |
| `HAL_CODEX_WORKSPACE_DIR` | `$CODEX_HOME/workspace` | `memory.jsonl` của realtime agent suy ra từ đây |

Những cái này hỏng ở rất xa nguyên nhân, nên phải set thành một khối chứ không
sửa lẻ từng cái: riêng TTS cache lộ ra dưới dạng `POST /voice/speak 409`, còn
`PermissionError: /var/lib/hal` thật thì nằm lẫn trong traceback của một thread
nền. Hai default còn lại là đường dẫn model chỉ-đọc (`/root/local/models`,
`/opt/piper`) — laptop không có thì tính năng cần chúng đơn giản là tắt.
`POST /audio/volume` trả 503 cũng là bình thường: macOS không có ALSA mixer.

Đặt credential vào chính config.json đó (Settings trên web UI ghi cùng file).
Riêng `llm_api_key` + `llm_base_url` đã phủ LLM, `AutonomousSTT`, TTS, mô tả ảnh
và cả Gemini Live — key của realtime fallback về `llm_api_key`, endpoint về
`llm_base_url` + `/ws/gemini` (`hal/config.py`), nên không cần credential Google
riêng. `deepgram_api_key` là tuỳ chọn.

Chép config.json của một thiết bị thật là cách nhanh nhất để có laptop full
option, nhưng phải xoá trắng hai key trước: `telegram_bot_token` (một bot không
thể có hai poller — laptop sẽ cướp tin nhắn của thiết bị) và `mqtt_endpoint`
(laptop sẽ subscribe đúng topic của thiết bị). Cả hai đều không phải năng lực
AI, nên không mất gì ở trên.

Servo ở đây không có thân máy vật lý: `http://127.0.0.1:5001/simulator` là chỗ
để xem, và nó gọi đúng các endpoint `/servo/*`, `/led/*` mà một skill gọi.

Hai điều cần biết trên macOS:

- Quyền Microphone và Camera phải được cấp cho ứng dụng terminal đang chạy HAL
  (System Settings > Privacy & Security). Liệt kê thiết bị không phải là quyền —
  danh sách vẫn hiện ra dù chưa cấp, chỉ lần đọc thật đầu tiên mới lỗi — nên HAL
  probe cả hai lúc boot và rơi về thiết bị ảo kèm log `[sim-media]` nói rõ lý do,
  thay vì để hỏng giữa một lượt nói.
- AirPlay Receiver cũng listen `*:5000`. os-server bind `127.0.0.1:5000`, nhưng
  request tới `localhost:5000` vẫn có thể rơi vào AirTunes — tắt receiver
  (System Settings > General > AirDrop & Handoff) hoặc đổi `httpPort`.
- `presync.sh` sinh lại `config.toml` mỗi lần boot và chỉ giữ `[mcp_servers.*]`.
  `os-dev-seed.sh` sao lưu file có sẵn thành `config.toml.pre-os-dev` một lần,
  nên trỏ `CODEX_HOME` vào một bản cài thật không phải đường một chiều.

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

### Reconcile USER.md theo enrollment

Lúc khởi động (sau persona migration) os-server retire người dùng khỏi `USER.md`
của **mọi** runtime một khi enrollment khuôn mặt/giọng nói của họ không còn.

`USER.md` là bootstrap file — được nhét vào system prompt của agent mỗi lượt —
nhưng chưa từng có thứ gì trên thiết bị ghi vào nó: agent ghi thứ nó học được vào
`KNOWLEDGE.md` và `memory/*.md`, mà OpenClaw không load file nào trong hai file
đó. File luôn được đọc lại là file không bao giờ được ghi, nên một thiết bị đã đổi
chủ vẫn gọi tên chủ cũ (lamp-ac82, 2026-09-03).

- **Quy tắc:** một cái tên chỉ cũ khi `usercanon.Resolve` ánh xạ nó tới thư mục
  không tồn tại trong `/root/local/users/`. **Vắng mặt không bao giờ là điều kiện
  kích hoạt** — người vắng một ngày hay một năm vẫn giữ enrollment, nên vẫn giữ
  profile. Chỉ `/face/remove`, `/speaker/remove` hoặc factory reset mới xoá.
- **Chỉ ghi khi có thay đổi.** `USER.md` nằm trong prefix prompt được cache
  (~28k token), nên ghi vô điều kiện sẽ tốn một lần miss cache ở lượt kế tiếp của
  mỗi lần boot. Lượt chạy bình thường đọc xong và không ghi gì.
- **Mặc định chỉ quan sát.** `user_profile_reconcile` trong `config.json` mở khoá
  việc ghi; không đặt/false thì chỉ log thứ nó *định* retire và không đổi gì.
- Ghi theo kiểu atomic (temp + rename) vì gateway đang chạy trong lúc pass chạy.
- Enrollment store rỗng (máy mới) là no-op; store không đọc được là lỗi và không
  đổi gì, thay vì đoán.

### Giữ hai file bộ nhớ không phình vô hạn

Chúng tốn token theo cách khác nhau, nên cũng bị chặn theo cách khác nhau.

| | Nằm trong system prompt? | Bị tính token | Trần |
|---|---|---|---|
| `USER.md` | **có** — là bootstrap file | **mỗi lượt** | 12000 ký tự (`bootstrapMaxChars`), vượt thì cắt từ đuôi |
| `KNOWLEDGE.md` | **không** — OpenClaw không biết file này | một lần mỗi session, khi agent đọc | không có |

`KNOWLEDGE.md` vốn không có trần nào: synthesis hằng ngày append thêm một block
`## YYYY-MM-DD` cho mỗi ngày hoạt động và không có gì xoá bớt. Đo trên lamp-ac82
là ~666 B/ngày — một năm dùng sẽ tới ~166 KB (~42k token) và bị đọc lại mỗi
session.

Hướng dẫn heartbeat giờ chặn lại: **giữ 14 block ngày gần nhất**, những gì cũ hơn
thì fold phần còn đúng vào các mục distilled ở đầu file (Hardware / Users /
Skills & APIs / Mistakes Made) rồi xoá block đó. Cách này dùng đúng cấu trúc sẵn
có — mục đầu file chính là *"Distilled from daily memory logs"*, còn các block
ngày là nguyên liệu thô — và ngày thô vẫn còn trong `memory/YYYY-MM-DD.md`.

### Đồng bộ người dùng hằng ngày (KNOWLEDGE.md → USER.md)

Lượt heartbeat có bước thứ hai sau knowledge synthesis: mang những gì học được về
*con người* sang `USER.md`.

Cả hai bước đều chạy theo kiểu **bù (catch-up), không theo đồng hồ**. Trước đây
synthesis bị gate bởi `current time >= 21:00`, và trên một thiết bị bị tắt cuối
giờ làm thì mốc đó âm thầm không bao giờ tới. Quan sát trên lamp-ac82 ngày
2026-09-03: ba ngày flow log kết thúc lúc 18:39 / 17:57 / 17:34, và
`memory/2026-08-24.md` chưa bao giờ được distil vì 21:00 không tới. Điều kiện giờ
là *"có ngày nào TRƯỚC hôm nay có memory file mà chưa có header `## YYYY-MM-DD`
không?"*, nên heartbeat đầu tiên sau khi bật máy sẽ dọn hết backlog, bất kể lịch
bật/tắt thế nào.

Lý do là một sự bất đối xứng đã gây bug thật. `KNOWLEDGE.md` là file của riêng
agent — **OpenClaw không load nó**; nó chỉ tới tay model khi agent chủ động đọc.
`USER.md` là bootstrap file, được nhét vào system prompt **mỗi lượt**. Vậy nên
file agent ghi hằng ngày lại là file hiếm khi được đọc, còn file luôn được đọc
thì không bao giờ được ghi: một thiết bị đã đổi chủ vẫn chào chủ cũ suốt hai
tháng.

Hướng dẫn nằm trong `heartbeatMDBlock` (`runtimes/<name>/onboarding.go`) và
giống hệt nhau từng byte ở openclaw / codex / opencode / picoclaw — đổi runtime
không được phép âm thầm làm mất nó.

| Quy tắc | Vì sao quan trọng |
|---|---|
| Mỗi người một bullet dưới `## Users`, dạng `- **<label> (friend)** — call: …; notes: …` | `<label>` là enrollment label lấy từ `[context: current_user=…]`, đúng khoá mà reconcile của OS dùng. Phần `(friend)` là thứ phân biệt một con người với một field biểu mẫu — thiếu nó, `**Notes:** …` sẽ bị đọc thành người tên "Notes:" và bị xoá. |
| Các đoạn `key: value` ngắn, không phải văn xuôi; `call:` đứng đầu | Các field của template là đơn nhất (một `**Name:**`, một `**Timezone:**`) nên không mô tả nổi hai người, nhưng lồng chúng theo từng người thì không sống sót qua file: `parseEntries` → `serialize` làm phẳng mọi bullet thành `- …`, nên field con thụt lề bị tách khỏi người của nó. Các đoạn giữ được *ý* của biểu mẫu — dữ kiện tách bạch, có nhãn — trong một entry prune được. Lần đầu để văn xuôi tự do đã cho ra một đoạn ~600 ký tự với cách xưng hô nằm lẫn ở câu thứ tư. |
| Không bao giờ đoán `call:`, đại từ nhân xưng hay múi giờ | Agent chỉ thấy một face label và một voiceprint. Không thứ nào nói lên người ta muốn được gọi thế nào. Chỉ ghi khi họ đã tự nói; nếu chưa, bỏ hẳn đoạn đó. |
| Mỗi entry dưới ~400 ký tự | `USER.md` bị tính token mỗi lượt, và vượt `bootstrapMaxChars` (12000) thì OpenClaw cắt bằng `text.slice(0, cutPoint)` — giữ đầu, **cắt đuôi** — mà `## Users` chính là phần đuôi. Profile phình to sẽ âm thầm mất đúng phần dữ liệu về người. `ReconcileUserProfiles` cảnh báo từ mốc 9000. |
| Người lạ không có entry | `## Users` khoá theo enrollment label; một khuôn mặt đi ngang không có label nào. Lưu lượng người qua bàn thì ghi ở `KNOWLEDGE.md`. |
| Chỉ ghi điều quan sát được về **chính** người đó | Lỗi ban đầu là hai người bị gộp thành một profile (`Long/Leo`). Không bao giờ chuyển thói quen của người này sang người khác. |
| Chỉ thêm và cập nhật — **không bao giờ xoá** | Vắng mặt không phải là rời đi. Retire một người là việc của OS (`ReconcileUserProfiles`, khoá theo enrollment), không phải của agent. |
| Không điền `**Name:**` và các field đơn giá trị khác | Chúng là đơn nhất, không biểu diễn được thiết bị nhiều người — điền từ quan sát trong ngày sẽ giật qua giật lại giữa các user. Ai đang có mặt lấy từ tag mỗi lượt. |

`TestHeartbeatPeopleSyncFormatMatchesTheReconciler` khoá định dạng được dạy với
parser của reconciler, để hai bên không trôi ra khỏi nhau thành các entry không
ai prune được.
