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
   a. Kết nối WiFi (connect-wifi CLI) — song song, một goroutine
      early-capture publish IP LAN (STA) vào setup state ngay khi wlan0 có
      IP (trước cả khi có internet), để Web UI đọc được lúc AP còn sống
      trong giây lát (xem "Tự Động Chuyển Hướng AP→STA")
   b. Chờ internet (poll 60s)
   c. Setup agent gateway
   d. Lưu config
   e. Chờ agent ready (poll 120s)
   f. Báo cáo backend (MQTT)
   g. SetUpCompleted = true
7. Nếu thất bại → quay lại AP mode
8. Web UI tự chuyển hướng browser sang http://<lan_ip>/setup ngay khi
   operator đã về Wi-Fi nhà (mDNS .local dùng làm kênh phụ)
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

## Tự Động Chuyển Hướng AP→STA (màn hình "joining Wi-Fi…")

Sau khi operator submit, trang Setup hiện màn hình "Your device is joining
Wi-Fi…" và cố **tự động chuyển hướng browser sang địa chỉ mới của thiết bị trên
Wi-Fi nhà** ngay khi reachable, để operator không phải tự đi tìm IP. Phần này
mô tả cơ chế chuyển hướng đó, tại sao trước đây nó bị kẹt, và đã thay đổi gì.

### Ràng buộc cốt lõi

Thiết bị có **một sóng Wi-Fi duy nhất (`wlan0`)**. Lúc provisioning nó chạy như
access point tại `192.168.100.1`. Để join mạng nhà, nó phải chuyển cùng sóng đó
AP→STA — việc này **tắt AP**. Ngay khi AP chết, browser (vẫn đang nối SSID của
AP) **mất toàn bộ kết nối mạng tới thiết bị** cho tới khi operator tự nối lại
Wi-Fi nhà. **Không có khoảng nào** browser ở trên cả 2 mạng cùng lúc.

Hệ quả: browser chỉ có thể biết IP LAN mới của thiết bị **trước** khi AP tắt,
hoặc qua mDNS **sau** khi operator đã về Wi-Fi nhà.

### Ba kênh chuyển hướng (`useSetupStatusPolling.ts`)

1. **Phase poll** — poll `GET /api/device/setup/status` qua AP IP khi AP còn
   sống. Đọc `phase` + `lan_ip`. Chết ngay khi AP tắt.
2. **LAN-IP probe** — khi đã biết `lan_ip`, probe `http://<lan_ip>/api/health`
   từ browser; khi thành công (operator đã về Wi-Fi nhà và thiết bị online) thì
   chuyển hướng sang `http://<lan_ip>/setup?<params>`. **Hoạt động trên mọi LAN
   bất kể mDNS** — đây là kênh fallback đáng tin.
3. **mDNS probe** — probe `http://<type>-<id>.local/api/health`. Chuyển hướng
   khi browser resolve được tên `.local`. **Im lặng thất bại khi router chặn
   mDNS multicast** (phổ biến ở nhiều router gia đình/văn phòng, và trên Android
   Chrome vốn không có mDNS gốc).

### Nguyên nhân gốc của bug "kẹt mãi mãi"

Hai lỗi độc lập khiến màn hình treo vô hạn dù thiết bị đã join Wi-Fi thành công:

1. **CSP chặn mọi probe cross-origin.** nginx của thiết bị trả
   `Content-Security-Policy: …; connect-src 'self' ws: wss:`. Trang Setup phục
   vụ từ AP IP, nên `'self'` là `http://192.168.100.1`. Cả probe `lan_ip`
   (`http://172.x.x.x/…`) lẫn probe mDNS (`http://…​.local/…`) đều là **origin
   khác**, nên browser từ chối `fetch` với *"Refused to connect because it
   violates the document's Content Security Policy"* — `mode: "no-cors"`
   **không** bỏ qua được CSP. Mọi probe chết trước khi rời browser.

2. **Kênh LAN-IP gần như không bao giờ có `lan_ip` để dùng.** `lan_ip` chỉ được
   publish vào setup state *sau khi* `SetupNetwork()` xong vòng chờ internet
   **tới 60s**. Nhưng AP tắt trong ~2s sau khi gọi hàm đó, nên phase poll chết
   từ lâu trước khi `lan_ip` tồn tại → kênh LAN-IP bị vô hiệu → chỉ còn kênh
   mDNS → mà trên mạng chặn mDNS thì kênh này cũng không resolve được. Kết quả:
   **không kênh nào chạy.**

Nên trên router chặn mDNS multicast (đúng case thực tế), trang kẹt vĩnh viễn ở
"joining Wi-Fi…" dù đã join hoàn toàn thành công.

### Cách sửa

| Tầng | Thay đổi | Tại sao |
|------|----------|---------|
| **CSP** (`imager/build*.sh`, `scripts/provision/setup.sh`, `scripts/maintenance/patch-security.sh`) | `connect-src 'self' ws: wss:` → `connect-src 'self' ws: wss: http:` | Cho browser `fetch` các probe cross-origin LAN-IP và `.local`. Phải dùng `http:` (không phải `http://*.local`) vì **CSP không biểu diễn được dải IP** — một token `http:` là cách duy nhất cho phép `http://<bất-kỳ-ip-lan>/…`, nên fix độc lập với subnet của khách (`172.x`, `192.168.x`, `10.x`). |
| **Backend** (`internal/device/service.go`) | Một goroutine poll `GetCurrentIP()` mỗi giây **song song với** `SetupNetwork()` và publish IP STA vào setup state ngay khi xuất hiện (bỏ qua IP AP `192.168.100.1`), trước khi vòng chờ internet 60s xong. | Cho FE **cửa sổ lớn nhất có thể** để đọc `lan_ip` trong khoảng overlap ngắn lúc nó còn poll AP — để kênh LAN-IP thực sự có IP mà chuyển hướng tới. Một guard giữ IP đã capture khỏi bị ghi đè thành chuỗi rỗng bởi lần đọc sau lúc AP đang teardown. |
| **Frontend** (`useSetupStatusPolling.ts`) | Chuyển hướng LAN-IP giờ carry `pathname + search` và nhắm tới `http://<lan_ip>/setup?<params>`. | Giữ các param do OS-server đẩy (`llm_api_key`, `device_id`, …) còn hiệu lực trên host mới, khớp với kênh mDNS. IP đọc động từ backend — **không hardcode subnet**. |
| **Frontend** (`Setup.tsx`) | Ô copy "save this address" giờ hiện **URL IP thô** (`http://<lan_ip>/setup`), chỉ fallback `.local` tới khi early-poll lấy được `lan_ip`. | IP resolve trên mọi LAN; tên `.local` thất bại trên router chặn mDNS — nên link safety-net thủ công phải ưu tiên IP. |
| **Frontend** (`Setup.tsx`) | Nút Copy thêm fallback `document.execCommand("copy")` (textarea ẩn) cho khi `navigator.clipboard` không có. | Trang Setup phục vụ qua HTTP thuần (`http://192.168.100.1`), nơi `navigator.clipboard` là `undefined` (cần secure context) — nên API mới im lặng không làm gì và nút không hoạt động. Đường legacy chạy được trên origin `http://`. |

### Đích chuyển hướng

Happy path giờ chuyển hướng tới **`http://<lan_ip>/setup?<params>`** (vd
`http://172.168.20.145/setup?…`) — IP thô, hoạt động bất kể mDNS. Tới khi
early-poll lấy được `lan_ip`, link copy thủ công fallback về
`http://<type>-<id>.local/setup?<params>`.

### Đánh giá & đánh đổi

- **Sửa được gì:** auto-redirect (và link copy thủ công) giờ hoạt động trên
  mạng chặn mDNS — đúng lỗi thực tế đã báo. Giải pháp **không phụ thuộc subnet**
  — không giả định dải IP private cụ thể nào.
- **Vẫn phụ thuộc gì:** auto-redirect qua kênh LAN-IP chỉ chạy nếu FE kịp
  capture `lan_ip` trong khoảng ~2s lúc AP còn sống. Goroutine early-capture
  tối đa hóa cửa sổ này nhưng không đảm bảo 100% nếu DHCP cấp IP chậm. **Link
  copy IP thủ công là fallback chắc chắn** — luôn hiện và luôn dùng IP thô, nên
  operator không bao giờ bị kẹt.
- **Đánh đổi bảo mật của `http:` trong CSP:** `connect-src http:` cho phép trang
  Setup `fetch` mọi origin HTTP thuần, không chỉ thiết bị. Chấp nhận được vì
  bundle Setup chỉ phục vụ trên LAN/AP, không gửi secret trong các health probe
  này, và CSP không có cách hẹp hơn để cho phép một IP LAN tùy ý. Ghi nhận tại
  `docs/security/CHECKLIST.md` (F9).
- **Tại sao không tránh hẳn việc tắt AP:** dùng 2 sóng hoặc đường có dây sẽ bỏ
  được ràng buộc, nhưng phần cứng đích chỉ có một sóng Wi-Fi — nên mô hình "biết
  IP trước khi AP chết, hoặc qua mDNS sau đó" là cố hữu.

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
| `os/services/internal/device/service.go` | Setup orchestration + goroutine early-capture IP LAN |
| `os/services/web/src/lib/setupBridge.ts` | Bridge sự kiện về cửa sổ cha (postMessage) |
| `os/services/web/src/pages/Setup.tsx` | UI wizard Setup + các điểm gọi emit bridge + link copy ưu tiên IP |
| `os/services/web/src/hooks/setup/useSetupStatusPolling.ts` | Auto-redirect AP→STA: phase poll + LAN-IP probe + mDNS probe |
| `os/services/internal/network/service.go` | WiFi connect, AP mode |
| `os/services/server/device/delivery/http/handler.go` | HTTP setup handler (goroutine async) |
| `os/services/server/config/config.go` | Config load/save |
| `imager/build-orangepi.sh`, `imager/build.sh`, `scripts/provision/setup.sh` | nginx config bake vào image (gồm CSP `connect-src`) |
| `scripts/maintenance/patch-security.sh` | Patch bảo mật OTA cho thiết bị đã provision (migrate CSP) |
