# Setup Flow — Documentation

## Overview

When Lumi is not yet configured (`SetUpCompleted = false`), the device runs in AP mode, serving a Web UI for user setup.

## Flow

```
1. Device boots → check config.json
2. Not set up → AP mode (WiFi hotspot)
3. User connects to WiFi → opens Web UI
4. Enters: WiFi SSID/password + LLM config + channel
5. POST /api/device/setup
6. Lumi Server processes (async):
   a. Connect WiFi (connect-wifi CLI)
   b. Wait for internet (poll 60s)
   c. Setup OpenClaw agent
   d. Save config
   e. Wait for agent ready (poll 120s)
   f. Report to backend (MQTT)
   g. SetUpCompleted = true
7. On failure → return to AP mode
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

**Response:** Returns immediately `{"status": 1}`. Setup runs async in a goroutine after 2s delay.

### POST /api/device/channel

Change messaging channel after setup is complete.

## Network Setup

1. Call `connect-wifi` CLI tool with SSID + password
2. Poll checks:
   - SSID match? (`iwgetid`)
   - Internet OK? (`ping`)
3. Timeout 60s → fail
4. Success → save SSID + password to config

## AP Mode

- When not set up or setup fails → automatically switches to AP mode
- Device broadcasts WiFi hotspot
- Web UI serves setup page
- `SwitchToAPMode()` in `internal/network/service.go`
- **LED indicator:** once HTTP server is listening, if `SetUpCompleted == false` lumi spawns a background goroutine (`waitAndPaintSetupReady` in `server/server.go`) that polls LeLamp `GET /health` once per second up to 30s. As soon as `health.led == true`, it fires `POST /led/solid` with `{"color":[255,255,255]}` to paint the strip solid white. The poll exists because lumi-server typically binds :5000 before LeLamp's FastAPI is up on :5001 (Python loads `rpi_ws281x`, SPI, audio, camera) — a fire-and-forget paint would silently drop on `connection refused`. White stays on until setup completes (agent flash + ambient repaint it). The booting blue-breathing still shows during init.

## Post-Setup

After `SetUpCompleted = true`:
1. Connect OpenClaw WebSocket
2. Connect MQTT (subscribe fa_channel)
3. Start voice pipeline (if Deepgram key present)
4. Start ambient idle behaviors
5. Start sensing loop

## Config

Config stored at `config/config.json`. Managed by `server/config/config.go`.

| Field | Description |
|-------|-------------|
| `SetUpCompleted` | `true` when setup is done |
| `NetworkSSID` | WiFi SSID |
| `NetworkPassword` | WiFi password |
| `LLMProvider` | anthropic, openai, google, ... |
| `LLMApiKey` | LLM API key |
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

| File | Role |
|------|------|
| `lumi/internal/device/service.go` | Setup orchestration |
| `lumi/internal/network/service.go` | WiFi connect, AP mode |
| `lumi/server/device/delivery/http/handler.go` | HTTP setup handler |
| `lumi/server/config/config.go` | Config load/save |
