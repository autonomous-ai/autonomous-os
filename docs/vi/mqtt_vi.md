# MQTT — Tài Liệu

## Tổng Quan

OS server sử dụng MQTT để giao tiếp với backend server (báo cáo trạng thái, nhận lệnh OTA, thêm channel).

- Client: Eclipse Paho autopaho (Go)
- Auto-reconnect khi mất kết nối
- Client ID format: `device-{DeviceID}`

## Cấu Hình

```json
// config/config.json
{
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/{device_id}",
  "fd_channel": "fd/{device_id}"
}
```

## Topics

| Topic | Hướng | Mô tả |
|-------|-------|-------|
| `fa_channel` | Server → Device | Lệnh từ backend (from-agent) |
| `fd_channel` | Device → Server | Phản hồi từ thiết bị (for-device) |

## Commands

### Envelope Format

```json
{
  "cmd": "info|add_channel|slack_event|slack_command|whatsapp_pair|claudecode_login|claudecode_login_code|ota|data",
  ...payload fields
}
```

### `info` — Báo cáo thông tin thiết bị

**Nhận:** `{"cmd": "info"}`

**Phản hồi (publish fd_channel):**
```json
{
  "device": "lamp",
  "type": "info",
  "version": "0.0.35",
  "id": "{DeviceID}",
  "mac": "{MAC address}",
  "time": "2026-03-26T17:00:00Z",
  "wakeword_enabled": false,
  "agent_runtime": "openclaw"
}
```

`agent_runtime` là backend agentic **đang thực sự chạy** (`openclaw` | `hermes` |
`picoclaw` | `codex` | `claudecode` | `opencode`) — resolve theo thứ tự `config.agent_runtime`,
rồi `gateway.default` trong `ROBOT.md` của device, cuối cùng mặc định `openclaw`.
Phản hồi còn kèm các field tùy chọn khi có: `hal_version`, `openclaw_version`,
`hermes_version`, `picoclaw_version`, `codex_version`, `claudecode_version`,
`opencode_version`, `local_ip`, `tts_provider`, `tts_voice`, `stt_language`, `timezone`,
`unsupported_channels`, `skills`. `wakeword_enabled` luôn có mặt và báo trạng thái
effective của wake-word gate top-level trong config (`true` hoặc `false`; config legacy
thiếu giá trị này sẽ báo `false`). `timezone` là múi giờ IANA **trực tiếp** của device (ví dụ
`Asia/Ho_Chi_Minh`), đọc tươi từ `/etc/timezone` (fallback về config), không chỉ là
bản ghi trong config. Cả sáu version per-runtime đều được probe lúc startup (mỗi cái
từ `--version` riêng) và bắn cạnh nhau; `agent_runtime` cho biết cái nào đang active.

`unsupported_channels` (bỏ qua khi rỗng) liệt kê các channel đã cấu hình trên thiết bị
mà runtime **đang active** không chạy được. Nó được `ChannelReconcile` điền sau khi
chuyển runtime — vd chuyển `openclaw` → `picoclaw` (chỉ telegram) khiến mọi `slack`/
`discord` đã cấu hình thành không hỗ trợ. Danh sách lấy từ
`config.channels_unsupported`, được `ChannelReconcile` ghi lại mỗi lần chuyển runtime.

`skills` (bỏ qua khi rỗng) là những skill runtime **đang active** hiện có — đúng bộ
mà panel Manage skills trên web hiển thị (`AgentGateway.ListSkills`). Shape là
`[{"name":"music","description":"Play music."}]`: chỉ name + description, không bao
giờ kèm cây file mà `GET /api/agent/skills` cũng trả. Cú ping HTTP mang y hệt mảng
này (cùng type `domain.SkillSummary` nên hai uplink không thể lệch nhau).
Best-effort — runtime không list được hoặc thư mục skill đọc lỗi thì bỏ field chứ
không làm fail uplink. Field nằm trên `MQTTInfoResponse` mà các reply `data` embed,
nhưng chỉ `handleInfo` set nó, nên reply `data` không bao giờ mang theo.

**HTTP backend ping mirror các field này.** Cú ping do device chủ động gửi
(`POST {llm_base}/ping`, build bởi `system/device.buildPingPayload`, gửi qua
`system/beclient`) mang cùng bộ field trạng thái thiết bị như uplink `info`
này — `local_ip`, `device`, `device_id`, `timezone`, `tts_provider`,
`tts_voice`, `stt_language`, `wakeword_enabled`, `hal_version`, `unsupported_channels` — cộng thêm
`agent_runtime` và `agent_runtime_version`. Khác với `info` (báo version của
mọi backend đã cài cạnh nhau), ping chỉ gửi **version của runtime đang
active**. Ping bắn ở: (1) ngay sau khi join WiFi lúc setup (status
`setting_up`, fire-and-forget — publish `local_ip` trước bước setup agent tốn
tới ~2 phút, để đường cứu popup Setup mô tả trong `docs/setup-flow.md` hoạt
động được), (2) một lần khi setup xong (status `working`), và (3) định kỳ từ
status reporter. Field nào backend không xài thì đơn giản là bị bỏ qua.

Ping còn mang thêm **`skills`** — những skill runtime đang chạy hiện có, đúng bộ
mà panel Manage skills trên web hiển thị (`AgentGateway.ListSkills`, cũng là thứ
`GET /api/agent/skills` trả về). Gửi ở mọi lần ping để index skill theo device
phía backend tự chữa, cùng lý do với `slack_team_id`. Shape là
`[{"name":"music","description":"Play music."}]` — **chỉ name + description**,
cố ý không kèm cây file mà endpoint đó cũng trả: ping bắn mỗi 15s nên gửi cả
tree thường xuyên như vậy là vô ích (pane detail trên web lấy riêng qua
`GET /api/agent/skills/files` khi cần). Best-effort — runtime không list được,
thư mục skill đọc lỗi, hoặc device chưa có skill nào thì field bị bỏ qua chứ
không làm fail cú ping vốn còn mang `local_ip` tối quan trọng cho setup.

**Ping sống sót qua việc đổi địa chỉ LAN.** Địa chỉ của thiết bị không cố định —
chuyển dây ethernet sang mạng khác, hoặc DHCP cấp lại lease, là đổi, trong khi
os-server vẫn đang chạy. `beclient` giữ các kết nối keep-alive gắn với địa chỉ
nguồn **cũ**, và chúng không fail nhanh: đường cũ đơn giản là biến mất nên không
có RST nào để quan sát, cú ping kế tiếp ghi vào hư không cho tới khi hết timeout
15s của client, log `ping failed` một lần cho mỗi kết nối chết. Vì vậy status
reporter so `local_ip` với tick trước và gọi `beclient.CloseIdleConnections()` khi
nó đổi, để cú ping ngay sau đó mở kết nối mới. Client cũng tự giữ transport riêng
(clone, `IdleConnTimeout` 30s) thay vì dùng `http.DefaultTransport`, nên việc xả
pool không đụng tới các HTTP user khác trong process.

### `add_channel` — Thêm messaging channel

**Nhận:**
```json
{
  "cmd": "add_channel",
  "channel": "telegram|slack|discord|whatsapp",
  "config": {
    // telegram: bot_token + chat_id
    // slack:    bot_token + app_token + channel_id        (socket mode, mặc định)
    // slack:    bot_token + mode:"http" + signing_secret  (+ webhook_path tùy chọn, mặc định /slack/events)
    // discord:  bot_token + guild_id  + user_id
    // whatsapp: user_id (số điện thoại E.164 — chỉ field này; bot tự login qua Baileys)
  }
}
```

**Các mode transport của Slack.** `mode` chọn cách OpenClaw nhận Slack events:

- **`socket`** (mặc định khi không có `mode`) — OpenClaw mở WebSocket outbound tới Slack; cần `app_token`. Các install hiện tại không bị ảnh hưởng.
- **`http`** — OpenClaw lắng nghe Slack Events API POST tại `webhook_path` (mặc định `/slack/events`) và re-verify chữ ký Slack bằng `signing_secret`; không dùng `app_token`. Một proxy public (bff-campaign-service) nhận HTTP event từ Slack rồi fan-out tới đúng thiết bị qua MQTT dưới dạng `slack_event` (xem bên dưới). HTTP mode là đường chịu được mất message vì Slack retry ~3 lần trong 5 phút khi delivery fail.

**Phản hồi (một message — telegram/slack/discord):**
```json
{
  "device": "lamp",
  "type": "add_channel",
  "channel": "telegram",
  "status": "success|failure",
  "error": "..."
}
```

**Capability gate.** `add_channel` nay nhận biết capability: khi runtime agent **đang
active** không chạy được channel được yêu cầu, thiết bị phản hồi `status:"failure"` kèm
mã lỗi ổn định `error:"channel_not_supported"` (map từ `domain.ErrChannelNotSupported`
qua `errors.Is`, giống cách `channel.refresh_config` map các sentinel của nó). Trước đây
mọi runtime đều âm thầm chấp nhận bất kỳ channel nào. Mỗi runtime tự khai báo
`SupportedChannels` của mình — vd `picoclaw` chỉ chạy telegram, nên
`slack`/`discord`/`whatsapp` trả về `channel_not_supported`.

**Phản hồi (streaming — whatsapp):** thiết bị publish một message fd_channel cho mỗi pairing event:

1. `{"status":"pairing_starting"}` — đã spawn CLI subprocess.
2. `{"status":"pairing_qr","pairing_qr_text":"<QR dạng unicode-block>","pairing_qr_format":"unicode_blocks_2x1","pairing_qr_seq":1,"pairing_expires_at":"<RFC3339>"}` — lặp tối đa 5 lần khi Baileys xoay QR (~20s mỗi lần).
3. Một event kết thúc:
   - `{"status":"success"}` — đã link; phát ra sau khi đợi 5 phút post-pair sync để Baileys load xong history/pre-keys.
   - `{"status":"timeout","error":"..."}` — user không scan kịp.
   - `{"status":"failure","error":"..."}` — CLI exit bất ngờ hoặc đang có pairing flow khác chạy.

Nếu Baileys đã có session trên đĩa (`<openclaw_config_dir>/credentials/whatsapp/default/creds.json`), thiết bị bỏ qua QR và chỉ publish `{"status":"success"}`.

### `whatsapp_pair` — Chạy lại WhatsApp pairing

Re-run QR-scan flow mà không re-bootstrap channel config. Dùng khi Baileys session bị mất và cần re-link.

**Nhận:** `{"cmd": "whatsapp_pair"}`

**Phản hồi (streaming):** cùng shape với whatsapp `add_channel` stream phía trên, nhưng `type:"whatsapp_pair"`. Timeout 120s (vs. 10 phút cho `add_channel`) — đường này không cài plugin hoặc restart gateway.

### `claudecode_login` / `claudecode_login_code` — claude.ai OAuth login (runtime claudecode)

Chạy login subscription claude.ai (`claude setup-token`) trên thiết bị có runtime
active là claudecode, để brain xác thực bằng tài khoản Claude của user thay vì
`llm_api_key`. Chỉ runtime claudecode hỗ trợ — các runtime khác trả lời một
failure one-shot `{"status":"failure","error":"claude login not supported on … backend"}`.
Xem `docs/vi/agentic/claudecode_vi.md` §"Auth".

**Nhận:** `{"cmd": "claudecode_login"}`

**Phản hồi (streaming, `type:"claudecode_login"`):**

1. `{"status":"pairing_starting"}`
2. `{"status":"pairing_url","login_url":"https://claude.ai/oauth/authorize?..."}` —
   user mở URL này trong browser, authorize, và copy code hiện ra.
3. terminal: `{"status":"success"}` (token được persist vào config.json
   `claude_code_oauth_token`; presync chuyển runtime sang subscription auth và
   bridge restart) · `{"status":"timeout","error":"no login within 10m0s"}` ·
   `{"status":"failure","error":"..."}`.

**Nhận (chặng hai):** `{"cmd": "claudecode_login_code", "code": "<code đã dán>"}` —
đưa code từ browser ngược lại flow đang chờ. Được ack bằng
`{"status":"code_accepted"}` (hoặc `{"status":"failure","error":"no claude login in progress"}`);
status terminal của chính flow vẫn về trên stream `claudecode_login`.

Khác `whatsapp_pair`, handler login không block MQTT dispatch trong lúc flow chạy
— code đến như một MQTT command thứ hai, thứ sẽ không bao giờ được dispatch nếu
handler đầu tiên giữ loop.

### `slack_event` — Forward một Slack Events API delivery (HTTP mode)

Được gửi bởi Slack-events proxy public (bff-campaign-service) khi Slack delivery một
Events API POST cho workspace mà thiết bị này sở hữu. Payload (bản forward nguyên văn
body + signature headers của HTTP request từ Slack) và wire shape MQTT không đổi — nhưng
**cách thiết bị xử lý nay rẽ nhánh theo runtime đang chạy** (handler type-assert agent
gateway sang `domain.SlackBridge`):

- **Runtime tự phục vụ Slack webhook** (không phải `SlackBridge` — hiện tại: OpenClaw) —
  hành vi không đổi: thiết bị POST nguyên văn body + signature headers tới `webhook_path`
  của gateway local (mặc định `http://127.0.0.1:18789/slack/events`), nơi re-verify chữ ký
  Slack bằng `signing_secret` đã chia sẻ. Ack fd_channel mang HTTP status của gateway. Chỉ
  liên quan khi slack channel của thiết bị được cấu hình `mode:"http"` (xem `add_channel`).
- **Runtime có native Slack support chỉ là Socket Mode** (implement `domain.SlackBridge`) —
  nhánh này **dùng chung cho mọi runtime kiểu này** (hermes là ví dụ hiện tại, không phải
  trường hợp đặc biệt): chỉ có Socket Mode nên nó **không có Slack webhook HTTP local**, vì
  vậy os-server **chính là** Slack frontend HTTP-mode cho nó. Nó tự parse event và chạy một
  turn (`HandleInboundSlack`). Reply được render **thẳng tới Slack qua Bot API**, không relay
  ngược qua MQTT, dùng **native streaming API** của Slack: `chat.startStream` (mở streaming
  message) → `chat.appendStream` (`markdown_text` tăng dần) → `chat.stopStream` (finalize),
  cùng `assistant.threads.setStatus` cho indicator "…is typing" native. Ack fd_channel vẫn
  trở thành `status:"success"` (`http_status` 200) ngay khi inbound turn được dispatch.
  Challenge `url_verification` thường kết thúc tại proxy public (proxy sở hữu Slack
  Request URL), nên ở đây xử lý phòng hờ và vẫn ack `success`.

**Nhận:**
```json
{
  "cmd": "slack_event",
  "event_id": "Ev123",
  "body": "<raw Slack JSON body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/json"
  }
}
```

Thiết bị dedup theo `event_id` bằng LRU in-memory 5 phút (khớp retry window của Slack) và
forward headers nguyên văn để signature check của OpenClaw validate được.

**Phản hồi (publish fd_channel):**
```json
{
  "channel": "slack",
  "type": "slack_event",
  "event_id": "Ev123",
  "status": "success|failure|skipped_duplicate",
  "error": "...",
  "http_status": 200,
  "info": { /* cùng metadata device/version như các ack khác */ }
}
```

Để proxy route event inbound về đúng thiết bị, mỗi `/ping` kèm `slack_team_id` — workspace
ID mà thiết bị tự resolve on-device qua Slack `auth.test` với `botToken` đã lưu (cache lại,
gửi đi sau khi resolve được).

### `slack_command` — Forward một Slack slash command (HTTP mode)

Được gửi bởi cùng Slack proxy (bff-campaign-service) khi Slack delivery một slash-command
invocation (`/openclaw`, `/new`, ...) cho workspace mà thiết bị này sở hữu. Được forward và
verify y hệt `slack_event`: thiết bị POST nguyên văn body + signature headers tới **cùng**
`webhook_path` của OpenClaw gateway (mặc định `http://127.0.0.1:18789/slack/events`) —
endpoint HTTP duy nhất của OpenClaw route event vs. command theo body shape (urlencoded
`command=` vs. JSON `type`) và reply cho user qua `response_url` của command. Chỉ liên quan
khi slack channel của thiết bị được cấu hình `mode:"http"` (xem `add_channel`).

**Nhận:**
```json
{
  "cmd": "slack_command",
  "event_id": "<trigger_id>",
  "body": "<raw urlencoded form body>",
  "headers": {
    "X-Slack-Signature": "v0=...",
    "X-Slack-Request-Timestamp": "...",
    "Content-Type": "application/x-www-form-urlencoded"
  }
}
```

Khác với `slack_event`: body là form slash-command urlencoded (mang `command`, `text`,
`response_url`, `trigger_id`, ...), `Content-Type` là `application/x-www-form-urlencoded`,
và slot `event_id` mang `trigger_id` của Slack (slash command không có `event_id`) — dùng
lại làm dedup key.

**Hỗ trợ runtime:** slash command vẫn **chỉ dành cho OpenClaw**. Hermes `SlackBridge`
hoãn slash command ở giai đoạn này (v1) — chỉ `slack_event` mới runtime-aware — nên trên
thiết bị hermes, `slack_command` vẫn đi theo đường local-webhook của OpenClaw mô tả ở trên.

**Phản hồi (publish fd_channel):** cùng dạng với `slack_event` nhưng `type:"slack_command"`.

### `data` — Envelope dữ liệu chung

Envelope chung mà `kind` chọn sub-handler tương ứng. Object `data` (tùy chọn) mang
các field riêng theo từng kind. Mọi kind đều phản hồi trên fd_channel cùng một dạng:
metadata device/version chuẩn cộng với `kind`, `status` (`success|failure`), `error`
(tùy chọn) và payload `data` (tùy chọn).

**Nhận:** `{"cmd": "data", "kind": "<kind>", "data": { ... }}`

| Kind | Mục đích | Field trong `data` |
|------|----------|--------------------|
| `tts.set` | Lưu cấu hình TTS voice/provider/language | `provider`, `voice`, `language` |
| `tts.preview` | Preview TTS một lần (không ghi config) | `text` (bắt buộc), tùy chọn `provider`/`voice`/`language` |
| `wakeword.gate` | Bật/tắt wake-word gate top-level (bất đồng bộ; ack `starting`) | `enabled` (boolean bắt buộc) |
| `timezone.set` | Áp dụng múi giờ IANA của device (bất đồng bộ; ack `starting`) | `timezone` (bắt buộc, ví dụ `Asia/Ho_Chi_Minh`) |
| `oauth.set` | Lưu/thay token OAuth cho một provider | `provider`, `access_token`, tùy chọn `refresh_token`/`token_type`/`expires_at`/`scopes`/`user_email`/`client_id` |
| `oauth.remove` | Xóa token OAuth đã lưu của provider | `provider` |
| `connector.set.<code>` | Lưu/thay credentials cho một connector (bất đồng bộ; ack `starting`) | `connector`, `auth_type`, tùy chọn `access_token`/`refresh_token`/`api_key`/`expires_in`/`expires_at`/`scopes`/`credentials`/`refresh` |
| `connector.remove.<code>` | Xóa credentials của một connector (bất đồng bộ; ack `starting`) | `connector` |
| `channel.refresh_config` | Áp dụng lại block config chuẩn của một channel (bất đồng bộ; ack `configuring`) | `channel` |
| `skills.install_store` | Cài MỘT skill từ catalog lên runtime đang chạy (bất đồng bộ; ack `starting`) | `id`, `name` tuỳ chọn |
| `skills.files` | Đọc file của một skill đã cài — danh sách, hoặc nội dung một file (đồng bộ) | `name`, `path` tuỳ chọn |
| `skills.uninstall` | Xoá một skill đã cài khỏi runtime đang chạy (đồng bộ) | `name` |
| `chat.file.get` | Lấy một file trên device mà turn đã gọi tên (đồng bộ) | `path` (bắt buộc), tuỳ chọn `session_id`/`run_id` |
| `chat.send` | Mở một turn của agent từ backend rồi stream ngược về (ack một run id, sau đó bắn `chat.event`) | `message` (bắt buộc), tuỳ chọn `image`/`file`/`session_id`/`speak` |
| `skills.save` | Ghi một skill soạn sẵn vào thư mục skill của runtime đang chạy (đồng bộ) | `name`, `description`, `instructions` |
| `skills.upload` | Cài một file `.md`, `.zip`, hoặc `.skill` vào runtime đang chạy (đồng bộ) | `filename`, `content_base64` |
| `system.info` | Snapshot tổng hợp: versions + network + host | _(không)_ |
| `system.version` | Chỉ versions các thành phần (rẻ hơn `system.info`) | _(không)_ |
| `system.network` | Chỉ thông tin mạng của interface đang giữ default route | _(không)_ |

**Phản hồi `system.info`:** đồng bộ (không có trạng thái `starting` trung gian); mỗi
probe lỗi sẽ rơi về zero value của nó.
```json
{
  "device": "lamp",
  "type": "data",
  "kind": "system.info",
  "status": "success",
  "data": {
    "versions": {
      "os-server": "0.0.35",
      "bootstrap": "0.0.10",
      "hal": "1.2.3",
      "openclaw": "2026.6.10",
      "openclaw_detected": true
    },
    "network": {
      "private_ip": "192.168.1.42",
      "interface": "wlan0",
      "mac": "aa:bb:cc:dd:ee:ff",
      "ssid": "MyWiFi",
      "gateway": "192.168.1.1"
    },
    "host": {
      "hostname": "lamp-7f72",
      "device_id": "{DeviceID}",
      "device_name": "lamp-7f72",
      "uptime_seconds": 86400,
      "timezone": "Asia/Ho_Chi_Minh"
    }
  }
}
```

Field `host.timezone` là múi giờ IANA **trực tiếp** của device, đọc tươi từ hệ thống
(`/etc/timezone`, fallback về config); bị bỏ qua khi không resolve được.

Block `network` mô tả **interface đang giữ default route**, không phải cứ `wlan0` —
`network.PrimaryInterface()` đọc từ `ip route show default`, nên device chạy dây ethernet
báo `"interface": "end0"` kèm `private_ip` và `mac` của link đó, còn `ssid` rỗng (nó không
associate WiFi nào cả). Khi không có default route — tức AP/provisioning mode — nó fallback
về `wlan0`, lúc này đang giữ `192.168.100.1` của chính AP. Trước đây interface bị hardcode
nên device chỉ cắm dây báo IP và MAC rỗng dù vẫn truy cập được bình thường.

`system.version` chỉ trả về block `versions` trong `data`; `system.network` chỉ trả
về block `network`. Cách probe version: `os-server` từ biến ldflags lúc build, `bootstrap`
qua `bootstrap-server --version`, `hal` qua HTTP từ endpoint `/version` của HAL
local, `openclaw` từ probe cache của agent monitor (`openclaw_detected` phân biệt
"chưa cài" với "đã cài nhưng không parse được").

`kind` không hợp lệ sẽ phản hồi `status:"failure"` kèm `error:"unknown kind: <kind>"`.

#### `wakeword.gate`

Bật hoặc tắt cờ `wakeword` top-level. Lệnh dùng cùng kiểu ack bất đồng bộ như
`realtime.set`: device ack đã nhận, lưu cờ vào `config.json`, restart HAL khi
giá trị thay đổi, rồi publish kết quả.

**Nhận:** `{"cmd":"data","kind":"wakeword.gate","data":{"enabled":true}}`

Ack `success` cuối cùng echo lại `{"enabled":true}`. Thiếu `enabled` hoặc JSON
không hợp lệ trả `status:"failure"`. `success` nghĩa là cờ đã được lưu và HAL
đang restart; không đợi HAL sẵn sàng.

#### `timezone.set`

Đặt múi giờ IANA của device. Cùng dạng bất đồng bộ như `realtime.set` / `tts.set`:
device ack ngay lập tức, áp dụng thay đổi ở background, rồi ack kết quả.

**Nhận:** `{"cmd": "data", "kind": "timezone.set", "data": {"timezone": "Asia/Ho_Chi_Minh"}}`

**Luồng ack** (mỗi ack trên fd_channel, mang metadata device/version chuẩn cộng
`kind:"timezone.set"`):

1. `{"status":"starting"}` — đã nhận lệnh, trước khi áp dụng.
2. Một ack kết thúc:
   - `{"status":"success","data":{"timezone":"Asia/Ho_Chi_Minh"}}` — đã áp dụng (zone
     yêu cầu được echo lại trong `data`).
   - `{"status":"failure","error":"..."}` — bị từ chối (ví dụ zone không tồn tại, hoặc
     payload JSON không hợp lệ).

**Áp dụng:** zone được validate với `/usr/share/zoneinfo` (zone không tồn tại →
`failure`), rồi device ghi lại symlink `/etc/localtime`, ghi `/etc/timezone`, chạy
`timedatectl set-timezone` best-effort, và lưu `timezone` vào `config.json`. Thay đổi
có hiệu lực **không cần restart HAL** — các clock helper của HAL đọc `/etc/timezone`
tươi mỗi lần gọi.

#### Connectors

`connector.set.<code>` / `connector.remove.<code>` được route theo prefix (connector
code là phần hậu tố). Một writer **data-driven** duy nhất (`connectorWriter`) xử lý
mọi connector; một map nhỏ các **writer đặc biệt** chiếm giữ vài code không thể biểu
diễn bằng entry HTTP MCP thường (hiện chỉ có `figma-api`, một MCP server stdio cục bộ).
Writer chung quyết định theo từng message — dựa trên payload — connector có phải MCP
server không và xác thực ra sao; **không cần cập nhật registry per-connector cho một
connector mới**.

**Lưu trữ:** mỗi connector lưu vào file riêng `<code>_access_tokens.json` trong
`workspace/configs/` (ghi atomic tmp+rename, mode 0600). Connector code được kiểm tra
theo `^[a-z0-9_-]{1,64}$` trước khi dùng làm tên file hay khóa `mcp.servers.<code>`,
nên code không tin cậy không thể thoát khỏi thư mục configs qua path traversal.

**Routing (theo payload `connector.set`):** backend đặt các khóa routing trong map
`credentials` của payload:

| Khóa `credentials` | Tác dụng |
|--------------------|----------|
| `mcp_url` | Có → connector MCP: ghi `mcp.servers.<code>` (`{type:"http", url, headers.Authorization}`) vào `openclaw.json` và restart gateway. Không có → connector chỉ-credential (vd `gmail`/`google_*`): lưu token, **không** ghi entry `openclaw.json`. |
| `mcp_auth_header` | `bearer_access_token` (mặc định) → `Authorization: Bearer <access_token>`; `bearer_api_key` → `Bearer <api_key>` (connector dùng khóa tĩnh, vd `ahrefs`); `header:<Name>` → header thô `<Name>: <token>` không prefix Bearer (token ưu tiên `api_key`, fallback `access_token`) cho provider không dùng Bearer, vd Figma PAT `header:X-Figma-Token`. Connector PAT relay `auth_type:"pat"` với token trong `api_key`. |

**Bảng fallback:** với các connector ra đời trước khi wire mang các khóa này
(`notion`, `asana`, `linear`, `github`, `ahrefs`), một bảng compile-in cung
cấp `mcp_url` + kiểu header từ catalog openclaw (`runtimes/openclaw/mcp.go`). Payload
**luôn thắng** — `mcp_url` trong payload override bảng fallback — nên bảng chỉ là lưới
an toàn cho di trú đến khi backend gửi các khóa routing.

**Writer đặc biệt:** `figma-api` dùng workaround cho allowlist của Figma MCP hosted —
một MCP server stdio cục bộ (`{command:"node", args:[wrapper], env:{FIGMA_ACCESS_TOKEN}}`)
với script Node wrapper được ghi ra đĩa trước khi ghi entry. Code của writer đặc biệt
bị loại (`reserved`) khỏi vòng quét refresh của writer chung để nó không ghi đè chúng
ở dạng sai (HTTP).

**Refresh:** loop refresh quét writer chung (glob `*_access_tokens.json`) cùng từng
writer đặc biệt, và chủ động xoay vòng entry nào có CẢ `refresh_token` LẪN
`refresh:true` (backend sở hữu quyền quyết định refresh qua cờ `refresh`) khi còn dưới
10 phút là hết hạn, qua endpoint backend `/connector/refresh-token`.

#### `channel.refresh_config`

Áp dụng lại block config chuẩn của một channel trên thiết bị đã onboard — cho các khách
hàng cũ có runtime config ra đời trước khi schema thêm field (vd block `socketMode` của
Slack, streaming dạng object, `dmPolicy`). Chỉ config: **không** cài plugin, không
bootstrap CLI, không pairing. Credentials đọc từ `config.json` trên thiết bị — **KHÔNG**
mang trong payload; thiết bị tự dựng `RefreshChannelRequest` per-channel từ config.json.

**Generic.** Refresh nay hoạt động cho `telegram`, `slack` và `discord` — trước đây chỉ
`slack` được wire, các channel khác trả về `channel_not_supported`. Capability gate vẫn
áp dụng: channel nào runtime **đang active** không chạy được sẽ trả `channel_not_supported`.

**Nhận:** `{"cmd": "data", "kind": "channel.refresh_config", "data": {"channel": "slack"}}`

**Luồng bất đồng bộ** — thiết bị ack `configuring` (không phải `starting`, vì channel đã
được set up trước đó; đây là re-apply), rồi chạy ghi config + restart gateway ở background
và publish trạng thái kết thúc:

```json
{
  "device": "lamp",
  "type": "data",
  "kind": "channel.refresh_config",
  "status": "configuring | success | failure",
  "error": "<code>",
  "data": { "channel": "slack", "runtime": "2026.6.10" }
}
```

`data.runtime` mang chuỗi version runtime đã phát hiện (rỗng nếu probe lỗi) để backend
đối chiếu kết quả refresh với các lần nâng cấp runtime. Mã lỗi (trong `error` khi
`status:"failure"`, map từ sentinel qua `errors.Is`):

| Mã | Ý nghĩa |
|----|---------|
| `slack_credentials_missing` | config.json không có credentials cho channel đang refresh (giữ lại để tương thích wire; áp dụng cho mọi channel, không chỉ slack) |
| `channel_not_supported` | runtime đang active không chạy được channel này |

#### `skills.install_store`

Bản MQTT của `POST /api/agent/skills/install` (nút Install trên web). Device tải
file `.skill` từ catalog và runtime **đang chạy** giải nén vào thư mục skill của
nó qua `AgentGateway.InstallSkillArchive`, nên chạy được trên mọi backend chứ
không riêng openclaw.

> Hậu tố `_store` chỉ vì kind `skills.install` trơn đã bị chiếm bởi chức năng cũ
> và khác hẳn: cả một ROLE bundle ghi thẳng vào `OpenclawConfigDir`.

**Nhận:** `{"cmd": "data", "kind": "skills.install_store", "data": {"id": "6a195e59e438b1a9f06299d0"}}`

`id` là id skill trong catalog. `name` là tuỳ chọn, **chỉ** dùng làm fallback khi
archive không có thư mục bọc duy nhất để lấy tên skill (bundle `.skill` của
catalog thường có, dạng `<name>/SKILL.md`).

**Bất đồng bộ** — phải tải qua mạng, nên device ack `starting` rồi publish status
cuối khi xong.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.install_store",
  "status": "starting | success | failure",
  "error": "<step>: <message>",
  "data": { "id": "6a19\u2026", "name": "design-critique", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/design-critique" }
}
```

`data.name` được đọc lại từ thư mục thực sự được tạo, nên không cần ai nói tên cho
device. `data.runtime` + `data.path` cho biết runtime nào lưu và lưu ở đâu — cả
hai khác nhau theo backend.

| `failed_step` | Nghĩa |
|---------------|-------|
| `validate_id` | id rỗng hoặc chứa ký tự phân cách đường dẫn (`/ \\ ? #`) |
| `temp_dir` | không tạo được thư mục staging |
| `download` | không tới được catalog, trả non-200, hoặc id không tồn tại |
| `archive` | file tải về không phải zip dùng được / rỗng |
| `validate_name` | tên skill suy ra từ archive không phải slug hợp lệ |
| `unsupported_runtime` | runtime đang chạy không có thư mục skill ghi được; **không cài gì cả** |
| `install` | lỗi giải nén / swap |

Install **thay thế** skill trùng tên — khác `skills.save` là từ chối ghi đè: cài
là chỉ thị chủ động, soạn thảo thì không. Giải nén được stage ở `<skill>.new` và
chỉ swap khi thành công trọn vẹn (bản cũ giữ ở `<skill>.old` cho tới lúc đó), nên
bản tải hỏng không bao giờ để lại skill cài dở hay phá skill đang chạy.

Đồng thời: dùng chung một mutex với `skills.install`, `skills.save`,
`skills.upload`, và `skills.uninstall` — cả năm cùng thay đổi một cây skills.
Cái thứ hai đến giữa lúc đang chạy sẽ fail ngay với
`"another skills install is already in progress; try again later"`.

Không restart gateway: mọi backend có thư mục skill đều nhặt file mới theo từng
session.

#### `skills.files`

Bản MQTT của `GET /api/agent/skills/files`. Endpoint đó chỉ trong LAN và sau
admin-auth, nên backend — và qua đó là app mobile — không có cách nào xem một skill
mà uplink `skills` đã báo có. Đây là đường vào đó.

**Hai chế độ**, vì MQTT không phải kênh truyền khối lớn và cả một skill có thể nặng
vài MB:

| Nhận | Trả về |
|------|--------|
| `{"name":"music"}` | **danh sách** file — `path` / `size` / `binary` mỗi entry, **không có nội dung** |
| `{"name":"music","path":"music/SKILL.md"}` | **một file** đó, nội dung inline |

`path` phải đúng y như danh sách đã báo (tương đối so với gốc thư mục skill, nên có
kèm tên thư mục skill). Truyền basename hay thử `..` đều không khớp — tra cứu là so
khớp chính xác với listing, không bao giờ join đường dẫn trên filesystem.
Khi có `path`, device chỉ đọc file đó; skill có nhiều reference/asset sẽ không
làm chậm phản hồi vì phải nạp mọi file còn lại trước.

**Đồng bộ** — đọc thư mục skill là đọc đĩa local, nên không có ack `starting`.

Chế độ danh sách:
```json
{
  "device": "lamp", "type": "data", "kind": "skills.files",
  "status": "success",
  "data": { "name": "music", "runtime": "OpenClaw", "files": [
    {"path": "music/SKILL.md", "size": 1204},
    {"path": "music/reference/tempo.md", "size": 380},
    {"path": "music/assets/icon.png", "size": 9001, "binary": true}
  ]}
}
```

Chế độ một file trả `data.file` thay vì `files`: cùng entry đó cộng `text`, và
`truncated: true` khi nội dung bị cắt.

**Ngân sách kích thước.** Uplink này trả tối đa **5 KiB** đầu tiên của text file
được yêu cầu; nếu bị cắt sẽ có `truncated: true`. Điểm cắt không bao giờ chẻ đôi
một rune nhiều byte nên text vẫn là UTF-8 hợp lệ. Entry binary chỉ có metadata,
không bao giờ có bytes.

| `failed_step` | Nghĩa |
|---------------|-------|
| `validate_name` | tên skill không phải slug hợp lệ |
| `not_found` | `path` yêu cầu không có trong skill đó |
| `unsupported_runtime` | runtime đang chạy không có thư mục skill đọc được |
| `read` | skill không còn (list cũ) hoặc đọc lỗi |

#### `skills.uninstall`

Bản MQTT của `DELETE /api/agent/skills`. Xoá skill khỏi thư mục skill mà runtime
**đang chạy** sở hữu, qua `AgentGateway.DeleteSkill`.

**Nhận:** `{"cmd": "data", "kind": "skills.uninstall", "data": {"name": "music"}}`

**Đồng bộ** — xoá một thư mục là đọc/ghi đĩa local, nên không có ack `starting`.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.uninstall",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "music", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/music" }
}
```

**Cố ý không idempotent:** skill chưa cài sẽ trả `failed_step: "not_found"` chứ
không phải success — để view cũ phía backend hoặc lệnh gửi trùng lộ ra, thay vì
được báo là đã xoá thành công một thứ chưa từng bị xoá.

| `failed_step` | Nghĩa |
|---------------|-------|
| `validate_name` | tên không phải slug hợp lệ — `..` hay `/` không bao giờ ra được ngoài thư mục skill |
| `not_found` | không có skill đó (hoặc path đó không phải thư mục skill) |
| `unsupported_runtime` | runtime đang chạy không có thư mục skill ghi được |
| `remove` | lỗi filesystem |

Đồng thời: dùng chung một mutex với các kind install/save/upload, nên uninstall không xen
vào giữa lúc đang giải nén vào cùng cây thư mục.

Trên Hermes, các root được thử theo thứ tự device-owned trước, khớp với thứ tự của
listing — nên uninstall xoá đúng skill mà uplink `skills` đã báo có.

Sau response `success`, device lập tức publish uplink MQTT `info` thông thường
với inventory `skills` đã cập nhật. Nếu uplink best-effort này lỗi thì chỉ log,
không biến kết quả uninstall thành thất bại.

#### `skills.save`

Ghi MỘT skill soạn sẵn vào thư mục skill mà agentic runtime **đang chạy** sở hữu.
Đây là bản MQTT của `POST /api/agent/skills`: cả hai đều gọi
`AgentGateway.SaveSkill`, nên skill do backend push vào nằm đúng chỗ với skill
viết từ form "Write skill" trên web, và tuân cùng quy tắc không ghi đè.

**Nhận:**
```json
{"cmd": "data", "kind": "skills.save", "data": {
  "name": "weekly-status-report",
  "description": "Summarise the week's activity into a short status report.",
  "instructions": "When the user asks for a weekly status report:\n1. …"
}}
```

`name` + `description` thành YAML front-matter của SKILL.md, `instructions` thành
phần body markdown (`skills.RenderSkillMarkdown`). Cả ba đều bắt buộc; mỗi cái
được trim trước, nên giá trị có khoảng trắng dư vẫn được nhận chứ không bị loại.

**Đồng bộ** — khác `skills.install`, không có ack `starting`: ghi một file chỉ mất
vài millisecond nên thiết bị publish luôn một status cuối.

```json
{
  "device": "lamp",
  "type": "data",
  "kind": "skills.save",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "weekly-status-report", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/weekly-status-report/SKILL.md" }
}
```

`data.runtime` cho biết runtime nào đã lưu và `data.path` là nơi nó nằm — cả hai
khác nhau theo backend, nên backend biết được cây nào đã nhận skill. Khi lỗi,
`data.failed_step` mang cùng nhãn với tiền tố trong `error`:

| `failed_step` | Nghĩa |
|---------------|-------|
| `validate_name` | tên không phải slug `^[a-z0-9_-]+$`, hoặc dài quá 64 ký tự |
| `already_exists` | đã có skill trùng tên — soạn thảo không bao giờ ghi đè |
| `unsupported_runtime` | runtime đang chạy không có thư mục skill ghi được; **không lưu gì cả** |
| `write` | lỗi filesystem |

Hình dạng tên được validate bên trong `SaveSkill` (qua `skills.ValidateSkillName`)
chứ không ở tầng MQTT, nên đường này và đường HTTP không bao giờ lệch nhau về
việc tên nào là hợp lệ.

Đồng thời: `skills.save` dùng chung mutex với `skills.install`,
`skills.upload`, `skills.install_store`, và `skills.uninstall`. Một lệnh save
đến giữa lúc đang install sẽ fail ngay với
`"a skills install is in progress; try again later"` thay vì chặn vòng dispatch
MQTT suốt thời gian tải từ CDN.

Sau response `success`, device lập tức publish uplink MQTT `info` thông thường
với inventory `skills` đã cập nhật. Nếu uplink best-effort này lỗi thì chỉ log,
không biến kết quả save thành thất bại.

Không restart gateway: mọi backend có thư mục skill đều nhặt file mới theo từng
session.

#### `skills.upload`

Cài file `.md`, `.zip`, hoặc `.skill` được đưa thẳng trong MQTT command. Đây là
bản MQTT của `POST /api/agent/skills/upload`: `.md` gọi
`AgentGateway.InstallSkillMarkdown`, còn `.zip`/`.skill` gọi
`AgentGateway.InstallSkillArchive`.

**Nhận:**
```json
{"cmd": "data", "kind": "skills.upload", "data": {
  "filename": "daily-note.md",
  "content_base64": "LS0tCm5hbWU6IGRhaWx5LW5vdGUKZGVzY3JpcHRpb246IENhcHR1cmUgYSBkYWlseSBub3RlLgotLS0KCiMgRGFpbHkgbm90ZQ=="
}}
```

`filename` là bắt buộc và extension phải là `.md`, `.zip`, hoặc `.skill`.
`content_base64` mang chính xác bytes của file; file sau giải mã tối đa 16 MiB
(vì vậy broker phải cho phép payload JSON/base64 khoảng 21.4 MiB ở ngưỡng này).
File `.md` phải là `SKILL.md` trơn hợp lệ, có `name` và `description` trong YAML
front-matter. Archive dùng cùng validation và cơ chế thay thế atomically như
luồng HTTP upload.

**Đồng bộ** — giải mã và cài đặt chạy trong MQTT dispatch path, nên không có ack
`starting`.

```json
{
  "device": "lamp", "type": "data", "kind": "skills.upload",
  "status": "success | failure",
  "error": "<step>: <message>",
  "data": { "name": "daily-note", "runtime": "OpenClaw",
            "path": "/root/.openclaw/workspace/skills/daily-note" }
}
```

Install thay thế atomically skill trùng tên. Khi lỗi, `data.failed_step` là một
trong các giá trị:

| `failed_step` | Nghĩa |
|---------------|-------|
| `validate_front_matter` | content không có YAML front-matter SKILL.md dùng được |
| `validate_name` | name trong front-matter không phải slug hợp lệ |
| `archive` | archive rỗng hoặc không có SKILL.md ở root |
| `unsupported_runtime` | runtime đang chạy không có thư mục skill ghi được |
| `install` | lỗi filesystem |

Đồng thời: dùng chung skills mutex với `skills.install`, `skills.install_store`,
`skills.save`, và `skills.uninstall`; command đến cùng lúc fail ngay thay vì xen
ghi vào cây skills của runtime.

Sau response `success`, device lập tức publish uplink MQTT `info` thông thường
với inventory `skills` đã cập nhật. Nếu uplink best-effort này lỗi thì chỉ log,
không biến kết quả upload thành thất bại.

#### `chat.send` + `chat.event`

Cho phép backend (và qua đó là app mobile) giữ **đúng cuộc hội thoại mà chat trên
web monitor đang giữ**. Chat web gồm 2 nửa — `POST /api/sensing/event` với
`type:"web_chat"` để mở turn (đường này forward y như `type:"mqtt_chat"`), và
SSE `GET /api/agent/events` để render — cả hai
đều nằm local trên device sau admin auth, nên điện thoại chạy 4G không với tới
cái nào. fa/fd sẵn là đường theo từng device và xuyên được NAT, nên cặp này đi
qua đó.

```
mobile ──HTTP──▶ backend ──fa: chat.send──▶ device
mobile ◀── SSE ── backend ◀── fd: chat.event × N ── device
```

**Nhận:**
```json
{"cmd": "data", "kind": "chat.send", "data": {
  "message": "cậu thấy gì?",
  "image": "<base64 jpeg, tuỳ chọn>",
  "file": {"name": "report.pdf", "mime": "application/pdf", "content": "<base64>"},
  "session_id": "abc123",
  "speak": false
}}
```

`message` bắt buộc. `image` chính là base64 mà chat web bỏ vào sensing event, nên
điện thoại đính ảnh y hệt cách đó.

`file` dành cho thứ **không phải ảnh** — PDF, CSV. Cố ý tách riêng field chứ
không nhét thêm vào `image`: ảnh phải đi qua gate describe-first của device, còn
tài liệu thì không được (đi qua là fail). File rơi vào `/tmp` với **đúng đuôi
thật**, và turn mang tag `[file: <path> (<name>)]` để agent mở được; `name` chỉ
dùng để lấy đuôi và làm nhãn hiển thị, không bao giờ là path, nên tên file độc
hại không lái được chỗ ghi. Giới hạn 10 MB sau khi decode
(`agentfile.InboundMaxBytes`), khớp với check phía composer web. `mime` chỉ mang
tính tham khảo — device không quyết định gì từ nó. Gửi cả hai field cùng lúc
cũng được. `session_id` device **không hiểu** gì về nó —
chỉ echo lại trên ack và trên mọi event để backend fan-out đúng client; device
**không** tách state hội thoại theo nó, vì chỉ có một agent và một history, y như
hai người cùng đứng cạnh cái máy. `speak` (mặc định false) cho device đọc to câu
trả lời; mặc định tắt vì người chat từ phòng khác không mong cái máy tự nhiên nói
— cũng chính là lý do chat web suppress TTS.

**Ack** (ngay lập tức, chỉ mang id để correlate — không mang câu trả lời):
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.send",
  "status": "success | failure",
  "data": { "run_id": "run-…", "session_id": "abc123" }
}
```

**Rồi tới một stream** `chat.event` do device tự bắn, mỗi monitor event của run
đó một message:
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.event", "status": "success",
  "data": { "run_id": "run-…", "session_id": "abc123",
            "event": { "id": "evt-42", "time": "…", "type": "assistant_delta",
                       "summary": "Đang chụp ảnh", "runId": "run-…" } }
}
```

`event` là `domain.MonitorEvent` **nguyên si** — đúng struct mà SSE của web đẩy
(`assistant_delta`, `thinking`, `tool_call`, `hw_*`, `token_usage`,
`chat_response`). Cố ý như vậy: client dùng lại luôn reducer của chat web, thay
vì một bộ vocabulary thứ hai mà chỉ cần thêm một event type là lệch nhau.

Vài điểm triển khai mà người viết backend cần biết:

- **Chỉ mirror run do backend mở.** Bus mang mọi turn trên device, kể cả turn nói
  bằng miệng; một run được track khi `chat.send` của nó được nhận và bỏ track ở
  event kết thúc, kèm TTL 10 phút cho turn chết giữa chừng không có event kết
  thúc.
- **Một turn bắn RẤT NHIỀU event `chat_response`, chỉ cái cuối mới là hết.**
  Runtime đẩy `chat_response` liên tục trong lúc câu trả lời đang stream — những
  cái trước mang `state` `"delta"`/`"partial"`, mỗi cái là một đoạn đầu dài dần
  của cùng câu trả lời. Run chỉ kết thúc ở `state` `"complete"`, `"final"` hoặc
  `"error"` (hoặc ở event `no_reply`, vốn bản chất chỉ bắn một lần). Client nào
  coi `chat_response` đầu tiên là hết thì mọi câu trả lời đều bị cắt còn mẩu đầu
  — đúng con bug bản đầu tiên mắc phải. Reducer của web monitor áp dụng đúng luật
  này (`ChatSection.tsx`), và đó chính là lý do dùng chung một bộ event.
- **`assistant_delta` được gộp** thành lô ~250 ms. Bus bắn một delta mỗi chunk của
  model, mà mỗi publish lên fd đều QoS 1 (một round-trip), nên forward 1:1 tốn
  hơn cả việc sinh ra chúng. Event đã gộp mang toàn bộ đoạn text tích luỹ,
  `detail.coalesced: true`, và `id` **rỗng** để không bị nhầm là replay của chunk
  cuối dùng để dựng nó. Text đang chờ luôn được flush **trước** mọi event khác,
  nên chip công cụ không bao giờ vượt lên trước câu văn đứng trước nó.
- **Turn đi ngược vào chính sensing endpoint của device qua loopback** thay vì gọi
  thẳng AgentGateway, để gate describe-first cho ảnh, nhánh queue lúc agent bận,
  việc mark run web-chat và flow logging đều là đúng code mà chat web đang chạy.
  Cùng lý do với hook gateway của Hermes POST vào `/api/agent/channel-turn`.
- **Forward bằng sensing type `mqtt_chat`, không phải `web_chat`.** Mọi gate xử lý
  hai type như nhau (`sensingmsg.IsChat`: suppress TTS, không wake vật lý, không
  opening filler, queue giống hệt khi agent bận, cùng prefix `[user] ` gửi model).
  Tách type chỉ để badge turn trên Flow Monitor cho biết **tin nhắn gõ ở đâu** —
  📱 `mqtt_chat` (app điện thoại) vs 🖥 `web_chat` (composer của monitor). Nếu
  `speak: true` thì turn forward thành `voice` và hiện như turn voice.
- **Một client broker riêng** (`device-<id>-chat`) được giữ mở cho stream. Helper
  `publish` dùng chung mở rồi đóng kết nối cho từng message — hợp lý với kết quả
  một-lần của command, nhưng thảm hoạ với hàng chục event mỗi turn. Client id
  phải khác: hai kết nối trùng id thì broker đá cái nối trước.
**File turn tạo ra — `chat.file.get`**

Một turn chỉ có thể GỌI TÊN file nó tạo: "chụp hình" thì kết thúc bằng path tuyệt
đối kiểu `/root/.openclaw/media/hal-snapshots/snap_*.jpg`. Client thấy path đó
trong message mình đang render rồi đi xin file — bản MQTT của việc chat web gọi
`GET /api/agent/file`.

**Client chủ động kéo, device không tự đẩy.** Cố ý giống hệt web: cách này chạy
được với cả message client **đã có sẵn** (hội thoại cuộn lại từ mấy tuần trước
vẫn hiện được ảnh — đẩy thì chỉ phủ đúng turn đang chạy), file không ai mở thì
không tốn gì trên uplink của device, và điện thoại dùng lại luôn regex path cùng
hành vi "hỏng thì để nguyên path dạng text" của client web thay vì viết bản thứ
hai.

**Nhận:**
```json
{"cmd": "data", "kind": "chat.file.get", "data": {
  "path": "/root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg",
  "session_id": "abc123",
  "run_id": "run-…"
}}
```

`path` bắt buộc. `session_id` / `run_id` device không hiểu gì, chỉ echo lại
nguyên vẹn để backend trả đúng client đã hỏi; cả hai tuỳ chọn, vì file có thể
được xin rất lâu sau khi run kết thúc.

**Trả về:**
```json
{
  "device": "<device_type>", "type": "data", "kind": "chat.file.get",
  "status": "success",
  "data": {
    "run_id": "run-…", "session_id": "abc123",
    "name": "snap_1785393455291.jpg",
    "path": "/root/.openclaw/media/hal-snapshots/snap_1785393455291.jpg",
    "mime": "image/jpeg", "size": 43165,
    "content": "<base64>"
  }
}
```

- **`path` do client gửi lên nên là input thù địch.** Thứ được phép rời device do
  `system/agentfile` quyết — đúng allow-list mà `GET /api/agent/file` đang
  enforce, cố ý chỉ một bản: allow-list có hai bản là hai cơ hội nới lỏng nhầm
  một bên. Root gồm `media/` + `workspace/` của từng runtime cộng `/tmp`; `.json`
  và `.log` không được serve (config JSON của runtime chứa gateway token); path
  phải resolve (`EvalSymlinks`) vào trong root, nên `..` lẫn symlink thoát ra đều
  chết.
- **Mọi trường hợp từ chối đều trả cùng một message**, `"file not available"`.
  Nói rõ là sai đuôi / ngoài root / không tồn tại thì kẻ dò biết được cấu trúc
  filesystem của device; lý do thật ghi vào log trên device.
- **`content` là base64**, đối xứng với cách `chat.send` mang ảnh vào, nên backend
  chỉ phải xử lý một kiểu encode cho cả hai chiều.
- **Quá 2 MB thì bỏ bytes, không bỏ record**: `too_large: true`, `content` rỗng,
  `size` vẫn thật, để client nói được "có file video 12 MB" thay vì không hiện
  gì. Ngưỡng đó là budget inline của MQTT, chặt hơn hẳn 32 MB của
  `agentfile.MaxBytes` (dành cho fetch HTTP cùng mạng) — đây là uplink của device
  dùng chung với mọi command khác, mà base64 còn cộng thêm 1/3.
- **Backend nên cache bytes.** Mỗi request là device đọc lại và encode lại từ
  đầu, mà file trong `/tmp` thì reboot là mất.

Thay thế: `integrations/chat-bridges/autonomous-chat-hook/` forward chat từ
backend một chiều dưới dạng `type:"voice"`, nên device đọc to câu trả lời và
không có gì quay về. Nó không thể làm nền cho một UI chat; cặp kind này thay nó ở
mục đích đó.

### `ota` — Trigger OTA update

Xử lý bởi bootstrap worker, không qua MQTT handler trực tiếp.

## Code

| File | Vai trò |
|------|---------|
| `system/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `system/lib/mqtt/config.go` | Config struct |
| `system/lib/mqtt/options.go` | Connection options |
| `system/lib/mqtt/factory.go` | Factory tạo client với unique ID |
| `system/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `system/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `system/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (stream pairing events cho WhatsApp) |
| `system/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` / `slack_command` (runtime-aware: forward Slack HTTP-mode events tới gateway OpenClaw local, hoặc drive hermes turn nếu runtime là `SlackBridge`) |
| `system/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `system/server/device/delivery/mqtt/skills_install_store_handler.go` | Handle `skills.install_store` (async catalog download → `AgentGateway.InstallSkillArchive`) |
| `system/server/device/delivery/mqtt/skills_upload_handler.go` | Handle `skills.upload` (SKILL.md inline → `AgentGateway.InstallSkillMarkdown`) |
| `system/server/device/delivery/mqtt/skills_files_handler.go` | Handle `skills.files` (đọc file của một skill đã cài: danh sách, hoặc nội dung một file) |
| `system/server/device/delivery/mqtt/skills_uninstall_handler.go` | Handle `skills.uninstall` |
| `system/server/device/delivery/mqtt/chat_send_handler.go` | Handle `chat.send` — forward turn qua loopback tới sensing endpoint |
| `system/server/device/delivery/mqtt/chat_stream.go` | Mirror monitor event của một chat run về dưới dạng `chat.event` |
| `system/server/device/delivery/mqtt/chat_file_handler.go` | Handle `chat.file.get` — validate path được yêu cầu rồi trả file về |
| `system/agentfile/agentfile.go` | Package quyết định file nào của device được phép đưa ra, kèm scanner tìm path cho client (dùng chung cho `chat.file.get` và `GET /api/agent/file`) |
| `system/server/device/delivery/mqtt/skills_save_handler.go` | Handle `skills.save` (ghi skill soạn sẵn, đồng bộ, qua `AgentGateway.SaveSkill`) |
| `system/server/device/delivery/mqtt/connector_handler.go` | Handle `connector.set.<code>`/`connector.remove.<code>` (bất đồng bộ, dispatch writer qua `connectorWriterFor`) |
| `system/server/device/delivery/mqtt/connector_writer.go` | Interface `ConnectorWriter` + file helpers `<code>_access_tokens.json` dùng chung |
| `system/server/device/delivery/mqtt/connector_writer_generic.go` | `connectorWriter` data-driven: routing MCP theo payload, bảng fallback, chặn path-traversal, token file per-connector |
| `system/server/device/delivery/mqtt/mcp_connector_writer.go` | Writer MCP stdio đặc biệt (`figma-api`): token file + entry MCP wrapper cục bộ trong `openclaw.json` |
| `system/server/device/delivery/mqtt/connector_refresh.go` | Loop refresh token connector (`/connector/refresh-token`) |
| `system/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `system/server/device/delivery/mqtt/channel_refresh_handler.go` | Handle `data` kind `channel.refresh_config` (re-apply block config của channel, bất đồng bộ) |
| `system/server/device/delivery/mqtt/timezone_set_handler.go` | Handle `data` kind `timezone.set` (áp dụng múi giờ IANA của device, bất đồng bộ) |
| `system/device/timezone.go` | `SetTimezone`/`CurrentTimezone`: validate zone, ghi lại `/etc/localtime` + `/etc/timezone`, `timedatectl` best-effort, lưu config |
| `system/device/channels.go` | `RefreshChannelConfig` (dựng request per-channel + capability gate) |
| `system/agent/channel_reconcile.go` | `ChannelReconcile`: áp dụng lại channel sau khi chuyển runtime, ghi `channels_unsupported` |
| `system/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `system/server/device/delivery/mqtt/claudecode_login_handler.go` | Handle `claudecode_login` / `claudecode_login_code` (claude.ai OAuth login) |
| `runtimes/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `system/domain/device.go` | MQTTMessage, command constants |
| `system/domain/pairing.go` | PairingEvent + status enum |
