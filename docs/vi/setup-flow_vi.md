# Setup Flow — Tài Liệu

## Tổng Quan

Khi OS server chưa được cấu hình (`SetUpCompleted = false`), thiết bị chạy ở chế độ AP mode, phục vụ Web UI để người dùng setup.

## Flow

```
1. Thiết bị khởi động → check config.json
2. Chưa setup → AP mode (WiFi hotspot)
3. Người dùng kết nối WiFi → mở Web UI
4. Nhập: WiFi SSID/password + LLM config + channel
5. POST /api/device/setup
6. OS Server xử lý (async):
   a. Kết nối WiFi (connect-wifi CLI)
   b. Chờ internet (poll 60s)
   c. Setup agent gateway
   d. Lưu config
   e. Chờ agent ready (poll 120s)
   f. Báo cáo backend (MQTT)
   g. SetUpCompleted = true
7. Nếu thất bại → quay lại AP mode
```

## API

### POST /api/device/setup

```json
{
  "network_ssid": "MyWiFi",
  "network_password": "...",
  "llm_provider": "anthropic",
  "llm_api_key": "sk-...",
  "llm_base_url": "https://api.anthropic.com",
  "llm_model": "claude-haiku-4-5-20251001",
  "channel_type": "telegram",
  "channel_token": "...",
  "channel_id": "...",
  "mqtt_endpoint": "broker.example.com",
  "mqtt_port": 8883,
  "mqtt_username": "...",
  "mqtt_password": "...",
  "fa_channel": "fa/device123",
  "fd_channel": "fd/device123",
  "deepgram_api_key": "..."
}
```

**Response:** Trả về ngay `{"status": 1}`. Setup chạy async trong goroutine sau 2s delay.

### POST /api/device/channel

Thay đổi messaging channel sau khi đã setup. Chấp nhận `telegram`, `slack`, `discord`.

**WhatsApp bị reject ở đây** (`400 whatsapp pairing not supported via HTTP; use MQTT add_channel`) — WhatsApp pairing stream rotating QR về caller, HTTP fire-and-forget không carry được. Đường chính tắc là MQTT `add_channel` command (xem `docs/mqtt.md`) — thiết bị publish một message fd_channel cho mỗi pairing event. Re-pair không re-bootstrap dùng MQTT `whatsapp_pair` command.

## Network Setup

1. Gọi `connect-wifi` CLI tool với SSID + password
2. Poll kiểm tra:
   - SSID match? (`iwgetid`)
   - Internet OK? (`ping`)
3. Timeout 60s → fail
4. Thành công → lưu SSID + password vào config

## AP Mode

- Khi chưa setup hoặc setup fail → tự động chuyển AP mode
- Thiết bị phát WiFi hotspot
- Web UI phục vụ trang setup
- `SwitchToAPMode()` trong `internal/network/service.go`
- **Tín hiệu LED:** ngay khi HTTP server bắt đầu listen, nếu `SetUpCompleted == false` thì OS server spawn goroutine background (`waitAndPaintSetupReady` trong `server/server.go`) poll `GET /health` của HAL mỗi giây tối đa 30s. Khi `health.led == true` thì fire `POST /led/solid` với `{"color":[255,255,255]}` paint strip trắng solid. Poll vì os-server bind :5000 thường nhanh hơn HAL FastAPI bind :5001 trên cold boot (Python load `rpi_ws281x`, SPI, audio, camera) — fire-and-forget paint sẽ rớt im lặng với `connection refused`. Trắng giữ đến khi setup xong (agent flash + ambient paint đè lên). Blue-breathing booting vẫn show trong lúc init.
- **Khử nhiễu LED trong AP mode:** openclaw WS reconnect loop (`internal/openclaw/service_ws.go`) skip Set/Clear `StateAgentDown` khi `config.SetUpCompleted == false`, để overlay cyan disconnect không đè lên trắng setup-needed lúc provisioning. WS vẫn chạy (`device.Setup` cần nó ready để `WaitForAgentReady` pass trước khi flip `SetUpCompleted=true`), chỉ gate side-effect LED thôi.

## Post-Setup

Sau khi `SetUpCompleted = true`:
1. Kết nối OpenClaw WebSocket
2. Kết nối MQTT (subscribe fa_channel)
3. Start voice pipeline (nếu có Deepgram key)
4. Start ambient idle behaviors
5. Start sensing loop

## Config

Config lưu tại `config/config.json`. Managed bởi `server/config/config.go`.

| Field | Mô tả |
|-------|-------|
| `SetUpCompleted` | `true` khi setup xong |
| `NetworkSSID` | WiFi SSID |
| `NetworkPassword` | WiFi password |
| `LLMProvider` | anthropic, openai, google, ... |
| `LLMApiKey` | API key cho LLM |
| `LLMBaseUrl` | LLM API base URL |
| `LLMModel` | Model name |
| `ChannelType` | telegram, slack |
| `ChannelToken` | Channel bot token |
| `ChannelID` | Channel/chat ID |
| `DeepgramApiKey` | Deepgram STT API key |
| `LocalIntent` | Enable/disable local intent matching (default: true) |
| `MQTTEndpoint` | MQTT broker host |
| `MQTTPort` | MQTT broker port |
| `FAChannel` | MQTT subscribe topic (server→device) |
| `FDChannel` | MQTT publish topic (device→server) |

## Bridge sự kiện về cửa sổ cha (parent window)

Khi trang Setup được mở dạng popup/iframe từ một site khác (ví dụ
`autonomous.ai`), nó báo từng cột mốc ngược về cửa sổ đã mở nó qua
`window.postMessage`. Đây là kênh cross-origin duy nhất hoạt động được
popup→opener, vì Setup được phục vụ từ AP IP của thiết bị
(`http://192.168.100.1`) hoặc host `<type>-<id>.local` — khác origin.

Cửa sổ cha nên truyền origin của mình để thiết bị biết gửi về đâu và payload
không bị broadcast ra `*`:

```js
const origin = encodeURIComponent(window.location.origin);
window.open(`http://192.168.100.1/setup?parent_origin=${origin}&...`, "_blank");
```

Thứ tự resolve origin: `?parent_origin=` → origin của `document.referrer` → `*`.

Mỗi message là một JSON envelope phẳng:
`{ source: "autonomous-device-setup", v: 1, event, ts, ...data }`. Lọc theo
`source` và switch theo `event`:

| `event` | Khi nào | Trường thêm |
|---------|---------|-------------|
| `setup_opened` | Wizard đã mount | `mode`, `deviceId`, `mac` |
| `step_changed` | Operator đổi bước wizard | `step` |
| `wifi_selected` | Đã chọn một mạng WiFi | `ssid` |
| `setup_submitted` | Bấm "Setup", chuẩn bị gửi request | `ssid`, `channel` |
| `setup_error` | Có lỗi validation/backend | `message` |
| `setup_connecting` | Thiết bị đang join WiFi (sau submit) | — |
| `setup_connected` | Thiết bị online + reachable | `mdns_host`, `lan_ip` |
| `setup_failed` | Join WiFi thất bại | `message` |
| `retry_clicked` | Bấm "Back to Wi-Fi" sau khi lỗi | — |
| `continue_clicked` | Bấm "Continue setup →" | `mdns_host` |
| `monitor_clicked` | Bấm "Go to monitor →" | — |

Các emit đều best-effort: không có opener/parent thì là no-op, và lỗi
postMessage bị nuốt, nên bridge không bao giờ ảnh hưởng tới luồng setup. Ví dụ
listener đầy đủ nằm ở phần header của file `lib/setupBridge.ts`.

## Code

| File | Vai trò |
|------|---------|
| `os/services/internal/device/service.go` | Setup orchestration |
| `os/services/web/src/lib/setupBridge.ts` | Bridge sự kiện về cửa sổ cha (postMessage) |
| `os/services/web/src/pages/Setup.tsx` | UI wizard Setup + các điểm gọi emit bridge |
| `os/services/internal/network/service.go` | WiFi connect, AP mode |
| `os/services/server/device/delivery/http/handler.go` | HTTP setup handler |
| `os/services/server/config/config.go` | Config load/save |
