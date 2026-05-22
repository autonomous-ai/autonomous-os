# Setup Flow — Tài Liệu

## Tổng Quan

Khi Lumi chưa được cấu hình (`SetUpCompleted = false`), thiết bị chạy ở chế độ AP mode, phục vụ Web UI để người dùng setup.

## Flow

```
1. Thiết bị khởi động → check config.json
2. Chưa setup → AP mode (WiFi hotspot)
3. Người dùng kết nối WiFi → mở Web UI
4. Nhập: WiFi SSID/password + LLM config + channel
5. POST /api/device/setup
6. Lumi Server xử lý (async):
   a. Kết nối WiFi (connect-wifi CLI)
   b. Chờ internet (poll 60s)
   c. Setup OpenClaw agent
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
- **Tín hiệu LED:** ngay khi HTTP server bắt đầu listen, nếu `SetUpCompleted == false` thì lumi spawn goroutine background (`waitAndPaintSetupReady` trong `server/server.go`) poll `GET /health` của LeLamp mỗi giây tối đa 30s. Khi `health.led == true` thì fire `POST /led/solid` với `{"color":[255,255,255]}` paint strip trắng solid. Poll vì lumi-server bind :5000 thường nhanh hơn LeLamp FastAPI bind :5001 trên cold boot (Python load `rpi_ws281x`, SPI, audio, camera) — fire-and-forget paint sẽ rớt im lặng với `connection refused`. Trắng giữ đến khi setup xong (agent flash + ambient paint đè lên). Blue-breathing booting vẫn show trong lúc init.
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

## Code

| File | Vai trò |
|------|---------|
| `lumi/internal/device/service.go` | Setup orchestration |
| `lumi/internal/network/service.go` | WiFi connect, AP mode |
| `lumi/server/device/delivery/http/handler.go` | HTTP setup handler |
| `lumi/server/config/config.go` | Config load/save |
