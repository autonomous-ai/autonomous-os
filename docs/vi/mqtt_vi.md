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
  "cmd": "info|add_channel|slack_event|slack_command|whatsapp_pair|ota|data",
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
  "agent_runtime": "openclaw"
}
```

`agent_runtime` là backend agentic **đang thực sự chạy** (`openclaw` | `hermes` |
`picoclaw`) — resolve theo thứ tự `config.agent_runtime`, rồi `gateway.default`
trong `DEVICE.md` của device, cuối cùng mặc định `openclaw`. Phản hồi còn kèm các
field tùy chọn khi có: `hal_version`, `openclaw_version`, `hermes_version`,
`local_ip`, `tts_provider`, `tts_voice`, `stt_language`, `unsupported_channels`.
`openclaw_version` và `hermes_version` đều được probe lúc startup (mỗi cái từ
`--version` riêng) và bắn cạnh nhau; `agent_runtime` cho biết cái nào đang active.

`unsupported_channels` (bỏ qua khi rỗng) liệt kê các channel đã cấu hình trên thiết bị
mà runtime **đang active** không chạy được. Nó được `ChannelReconcile` điền sau khi
chuyển runtime — vd chuyển `openclaw` → `picoclaw` (chỉ telegram) khiến mọi `slack`/
`discord` đã cấu hình thành không hỗ trợ. Danh sách lấy từ
`config.channels_unsupported`, được `ChannelReconcile` ghi lại mỗi lần chuyển runtime.

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
| `oauth.set` | Lưu/thay token OAuth cho một provider | `provider`, `access_token`, tùy chọn `refresh_token`/`token_type`/`expires_at`/`scopes`/`user_email`/`client_id` |
| `oauth.remove` | Xóa token OAuth đã lưu của provider | `provider` |
| `connector.set.<code>` | Lưu/thay credentials cho một connector (bất đồng bộ; ack `starting`) | `connector`, `auth_type`, tùy chọn `access_token`/`refresh_token`/`api_key`/`expires_in`/`expires_at`/`scopes`/`credentials`/`refresh` |
| `connector.remove.<code>` | Xóa credentials của một connector (bất đồng bộ; ack `starting`) | `connector` |
| `channel.refresh_config` | Áp dụng lại block config chuẩn của một channel (bất đồng bộ; ack `configuring`) | `channel` |
| `system.info` | Snapshot tổng hợp: versions + network + host | _(không)_ |
| `system.version` | Chỉ versions các thành phần (rẻ hơn `system.info`) | _(không)_ |
| `system.network` | Chỉ thông tin mạng wlan0 | _(không)_ |

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
      "openclaw": "2026.5.27",
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
      "uptime_seconds": 86400
    }
  }
}
```

`system.version` chỉ trả về block `versions` trong `data`; `system.network` chỉ trả
về block `network`. Cách probe version: `os-server` từ biến ldflags lúc build, `bootstrap`
qua `bootstrap-server --version`, `hal` qua HTTP từ endpoint `/version` của HAL
local, `openclaw` từ probe cache của agent monitor (`openclaw_detected` phân biệt
"chưa cài" với "đã cài nhưng không parse được").

`kind` không hợp lệ sẽ phản hồi `status:"failure"` kèm `error:"unknown kind: <kind>"`.

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
cấp `mcp_url` + kiểu header từ catalog openclaw (`internal/openclaw/mcp.go`). Payload
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
  "data": { "channel": "slack", "runtime": "2026.5.27" }
}
```

`data.runtime` mang chuỗi version runtime đã phát hiện (rỗng nếu probe lỗi) để backend
đối chiếu kết quả refresh với các lần nâng cấp runtime. Mã lỗi (trong `error` khi
`status:"failure"`, map từ sentinel qua `errors.Is`):

| Mã | Ý nghĩa |
|----|---------|
| `slack_credentials_missing` | config.json không có credentials cho channel đang refresh (giữ lại để tương thích wire; áp dụng cho mọi channel, không chỉ slack) |
| `channel_not_supported` | runtime đang active không chạy được channel này |

### `ota` — Trigger OTA update

Xử lý bởi bootstrap worker, không qua MQTT handler trực tiếp.

## Code

| File | Vai trò |
|------|---------|
| `os/services/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `os/services/lib/mqtt/config.go` | Config struct |
| `os/services/lib/mqtt/options.go` | Connection options |
| `os/services/lib/mqtt/factory.go` | Factory tạo client với unique ID |
| `os/services/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `os/services/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `os/services/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (stream pairing events cho WhatsApp) |
| `os/services/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` / `slack_command` (runtime-aware: forward Slack HTTP-mode events tới gateway OpenClaw local, hoặc drive hermes turn nếu runtime là `SlackBridge`) |
| `os/services/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `os/services/server/device/delivery/mqtt/connector_handler.go` | Handle `connector.set.<code>`/`connector.remove.<code>` (bất đồng bộ, dispatch writer qua `connectorWriterFor`) |
| `os/services/server/device/delivery/mqtt/connector_writer.go` | Interface `ConnectorWriter` + file helpers `<code>_access_tokens.json` dùng chung |
| `os/services/server/device/delivery/mqtt/connector_writer_generic.go` | `connectorWriter` data-driven: routing MCP theo payload, bảng fallback, chặn path-traversal, token file per-connector |
| `os/services/server/device/delivery/mqtt/mcp_connector_writer.go` | Writer MCP stdio đặc biệt (`figma-api`): token file + entry MCP wrapper cục bộ trong `openclaw.json` |
| `os/services/server/device/delivery/mqtt/connector_refresh.go` | Loop refresh token connector (`/connector/refresh-token`) |
| `os/services/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `os/services/server/device/delivery/mqtt/channel_refresh_handler.go` | Handle `data` kind `channel.refresh_config` (re-apply block config của channel, bất đồng bộ) |
| `os/services/internal/device/service.go` | `RefreshChannelConfig` (dựng request per-channel + capability gate) |
| `os/services/internal/agent/channel_reconcile.go` | `ChannelReconcile`: áp dụng lại channel sau khi chuyển runtime, ghi `channels_unsupported` |
| `os/services/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `os/services/internal/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `os/services/domain/device.go` | MQTTMessage, command constants |
| `os/services/domain/pairing.go` | PairingEvent + status enum |
