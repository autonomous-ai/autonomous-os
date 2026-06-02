# MQTT — Tài Liệu

## Tổng Quan

Lamp sử dụng MQTT để giao tiếp với backend server (báo cáo trạng thái, nhận lệnh OTA, thêm channel).

- Client: Eclipse Paho autopaho (Go)
- Auto-reconnect khi mất kết nối
- Client ID format: `lamp-device-{DeviceID}`

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
  "cmd": "info|add_channel|slack_event|whatsapp_pair|ota|data",
  ...payload fields
}
```

### `info` — Báo cáo thông tin thiết bị

**Nhận:** `{"cmd": "info"}`

**Phản hồi (publish fd_channel):**
```json
{
  "device": "ai-lamp",
  "type": "info",
  "version": "0.0.35",
  "id": "{DeviceID}",
  "mac": "{MAC address}",
  "time": "2026-03-26T17:00:00Z"
}
```

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
  "device": "ai_lamp",
  "type": "add_channel",
  "channel": "telegram",
  "status": "success|failure",
  "error": "..."
}
```

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
Events API POST cho workspace mà thiết bị này sở hữu. Payload là bản forward nguyên văn
body + signature headers của HTTP request từ Slack; thiết bị POST chúng tới `webhook_path`
của OpenClaw gateway local (mặc định `http://127.0.0.1:18789/slack/events`), nơi re-verify
chữ ký Slack bằng `signing_secret` đã chia sẻ. Chỉ liên quan khi slack channel của thiết bị
được cấu hình `mode:"http"` (xem `add_channel`).

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
| `system.info` | Snapshot tổng hợp: versions + network + host | _(không)_ |
| `system.version` | Chỉ versions các thành phần (rẻ hơn `system.info`) | _(không)_ |
| `system.network` | Chỉ thông tin mạng wlan0 | _(không)_ |

**Phản hồi `system.info`:** đồng bộ (không có trạng thái `starting` trung gian); mỗi
probe lỗi sẽ rơi về zero value của nó.
```json
{
  "device": "ai_lamp",
  "type": "data",
  "kind": "system.info",
  "status": "success",
  "data": {
    "versions": {
      "lamp": "0.0.35",
      "bootstrap": "0.0.10",
      "lelamp": "1.2.3",
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
      "hostname": "ai-lamp",
      "device_id": "{DeviceID}",
      "device_name": "Lamp-7f72",
      "uptime_seconds": 86400
    }
  }
}
```

`system.version` chỉ trả về block `versions` trong `data`; `system.network` chỉ trả
về block `network`. Cách probe version: `lamp` từ biến ldflags lúc build, `bootstrap`
qua `bootstrap-server --version`, `lelamp` qua HTTP từ endpoint `/version` của LeLamp
local, `openclaw` từ probe cache của agent monitor (`openclaw_detected` phân biệt
"chưa cài" với "đã cài nhưng không parse được").

`kind` không hợp lệ sẽ phản hồi `status:"failure"` kèm `error:"unknown kind: <kind>"`.

### `ota` — Trigger OTA update

Xử lý bởi bootstrap worker, không qua MQTT handler trực tiếp.

## Code

| File | Vai trò |
|------|---------|
| `lamp/lib/mqtt/client.go` | MQTT client (connect, subscribe, publish) |
| `lamp/lib/mqtt/config.go` | Config struct |
| `lamp/lib/mqtt/options.go` | Connection options |
| `lamp/lib/mqtt/factory.go` | Factory tạo client với unique ID |
| `lamp/server/device/delivery/mqtt/handler.go` | Command dispatcher |
| `lamp/server/device/delivery/mqtt/info_handler.go` | Handle `info` command |
| `lamp/server/device/delivery/mqtt/add_channel_hander.go` | Handle `add_channel` command (stream pairing events cho WhatsApp) |
| `lamp/server/device/delivery/mqtt/slack_event_handler.go` | Handle `slack_event` command (forward Slack HTTP-mode events tới gateway local) |
| `lamp/server/device/delivery/mqtt/data_handler.go` | Handle `data` command kinds `oauth.set`/`oauth.remove` (+ access-token store) |
| `lamp/server/device/delivery/mqtt/system_info_handler.go` | Handle `data` kinds `system.info`/`system.version`/`system.network` |
| `lamp/server/device/delivery/mqtt/whatsapp_pair_handler.go` | Handle `whatsapp_pair` re-pair command |
| `lamp/internal/openclaw/pairing.go` | WhatsApp Baileys QR pairing subprocess driver |
| `lamp/domain/device.go` | MQTTMessage, command constants |
| `lamp/domain/pairing.go` | PairingEvent + status enum |
