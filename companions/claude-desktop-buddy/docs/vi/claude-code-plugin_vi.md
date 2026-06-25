# Plugin Claude Code (`claude-code-buddy/`)

Daemon trên thiết bị đã bắc cầu giữa **Claude Desktop** (Mac) và thiết bị qua
**Bluetooth LE** — heartbeat, sự kiện chat, và yêu cầu cấp quyền đi vào qua liên
kết Nordic UART rồi trở thành phản hồi LED / màn hình / giọng nói (xem
[`architecture_vi.md`](architecture_vi.md)).

Plugin **Claude Code** anh em tại [`../../claude-code-buddy/`](../../claude-code-buddy/)
là đối tác **Claude Code / HTTP** của con đường BLE đó. Thay vì ghép cặp qua
Bluetooth, nó chạy trên máy Mac của người dùng dưới dạng plugin Claude Code và
**PUSH** hoạt động của Claude Code tới HTTP API của thiết bị ở cổng `:5002`. Daemon
sẽ chuyển các push này thành chính những phản hồi (LED / màn hình / giọng nói) mà
nó vốn đã tạo ra cho luồng BLE.

## Cách nó bổ trợ cho con đường BLE

```
 Mac                                Thiết bị (Pi / OrangePi)
 ┌────────────────────┐  BLE/NUS   ┌──────────────────────────────────────┐
 │  Claude Desktop     │ ─────────►│  daemon claude-desktop-buddy          │
 │  (Hardware Buddy)   │           │   BLE ─► state machine ─► bridge ─┐    │
 └────────────────────┘           │                                   ▼    │
                                   │                       phản hồi thiết bị│
 ┌────────────────────┐  HTTP      │   httpapi/ :5002 ────────────────┘     │
 │  Claude Code        │  POST     │   /claude-code/*    (notify/usage)     │
 │  (claude-code-buddy)│ ─────────►│   /status /health /claude-desktop/*    │
 └────────────────────┘  :5002     └──────────────────────────────────────┘
```

Cả hai con đường đều dẫn tới **cùng** hành vi của thiết bị. Con đường BLE là một
liên kết liên tục, đã bond (bonded), do Claude Desktop điều khiển; con đường HTTP
là một tập hợp các push một lần, do các hook của Claude Code điều khiển. Plugin
không bao giờ chạm trực tiếp vào phần cứng — nó chỉ POST các sự kiện, và thiết bị
tự quyết định cách phản ứng.

## Hợp đồng push qua `:5002`

Plugin gửi JSON tới `http://<device>:5002`. Daemon trả về `{"ok":true}` khi thành
công.

### `POST /claude-code/notify`

Một tín hiệu rời rạc: Claude đã xong, cần bạn, hoặc một thông điệp tùy chỉnh.

```json
{
  "title": "Claude is done",
  "subtitle": "auth refactor",
  "level": "done",
  "sound": true
}
```

| Trường | Kiểu | Ghi chú |
|--------|------|---------|
| `title` | string | Tiêu đề hiển thị / đọc lên trên thiết bị |
| `subtitle` | string | Dòng phụ, tùy chọn |
| `level` | string | Một trong `"done"`, `"attention"`, `"info"` — chọn kiểu phản hồi |
| `sound` | bool | Thiết bị có thêm tín hiệu âm thanh hay không |

```bash
curl -s -X POST http://my-device.local:5002/claude-code/notify \
  -H 'Content-Type: application/json' \
  -d '{"title":"Claude is done","subtitle":"auth refactor","level":"done","sound":true}'
```

### `POST /claude-code/usage`

Mức sử dụng Claude Code hiện tại, push khi nó vượt ngưỡng (hoặc theo yêu cầu).

```json
{
  "five_hour": 72,
  "seven_day": 40,
  "reset_5h": "3:00 PM",
  "reset_7d": "Mon",
  "sound": false
}
```

| Trường | Kiểu | Ghi chú |
|--------|------|---------|
| `five_hour` | int | Phần trăm mức dùng trong 5 giờ (0–100) |
| `seven_day` | int | Phần trăm mức dùng trong 7 ngày (0–100) |
| `reset_5h` | string | Thời điểm cửa sổ 5 giờ reset |
| `reset_7d` | string | Thời điểm cửa sổ 7 ngày reset |
| `sound` | bool | Thiết bị có thêm tín hiệu âm thanh hay không |

```bash
curl -s -X POST http://my-device.local:5002/claude-code/usage \
  -H 'Content-Type: application/json' \
  -d '{"five_hour":72,"seven_day":40,"reset_5h":"3:00 PM","reset_7d":"Mon","sound":false}'
```

### Các endpoint daemon đã có

Những endpoint sau được gói `httpapi/` của daemon phục vụ và dùng cho luồng
BLE/cấp quyền: `GET /status`, `GET /health`, `POST /claude-desktop/approve`,
`POST /claude-desktop/deny`. Xem [`architecture_vi.md`](architecture_vi.md).

## Khám phá (discovery) và cấu hình

- **Khám phá** — plugin tìm thiết bị qua **mDNS** với loại dịch vụ
  `_autonomous._tcp` (cùng bộ quảng bá mà thiết bị vốn đã chạy), nên không cần gõ
  mã nào. Địa chỉ phân giải được cộng với cổng `:5002` chính là đích của các push.
- **Cấu hình (Mac)** — lưu tại `~/.config/claude-code-buddy.json`. Nó ghi địa chỉ
  thiết bị và các tùy chọn notify/usage/sound của plugin. Plugin tự sửa file này
  khi bạn yêu cầu bằng ngôn ngữ tự nhiên ("mute my device", "warn me earlier");
  không cần khởi động lại.

## Các thành phần của plugin

| Thành phần | Vai trò |
|------------|---------|
| **Hooks** | `Stop` → `POST /claude-code/notify` (`level":"done"`) cộng một `POST /claude-code/usage`; `Notification` → `POST /claude-code/notify` (`level":"attention"`) |
| `scripts/buddy_client.py` | HTTP client tối giản thực hiện các POST (chỉ dùng thư viện chuẩn Python 3) |
| `scripts/discover.py` | Bộ phân giải mDNS cho `_autonomous._tcp` |
| Lệnh `/claude-code-buddy:usage` | Push mức sử dụng hiện tại tới thiết bị ngay |
| Lệnh `/claude-code-buddy:notify` | Gửi một thông báo một lần tới thiết bị |

## Cài đặt / sử dụng

Từ Claude Code trên máy Mac:

```bash
claude plugins marketplace add https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/companions/claude-desktop-buddy/claude-code-buddy/.claude-plugin/marketplace.json
claude plugins install claude-code-buddy
```

Khởi động lại Claude Code, rồi kết nối thiết bị (mDNS sẽ tìm nó trong mạng LAN):

```
connect my device
```

Sau đó, các hook `Stop` và `Notification` sẽ push tự động, và bạn có thể push theo
yêu cầu bằng `/claude-code-buddy:usage` hoặc `/claude-code-buddy:notify` (hoặc ngôn
ngữ tự nhiên: "push my usage to my device", "notify my device"). Hướng dẫn cài đặt
đầy đủ của plugin nằm tại
[`../../claude-code-buddy/GUIDE.md`](../../claude-code-buddy/GUIDE.md).

## Khắc phục sự cố: Local Network trên macOS

macOS gần đây (Sonoma / Sequoia) chặn ứng dụng truy cập thiết bị trong mạng LAN
cho tới khi được cấp quyền **Local Network**. Plugin chạy bằng `python3`, nên nếu
thiếu quyền này, Python không thể tới được thiết bị — dù thiết bị đang online và
`curl` vẫn tới được bình thường.

**Triệu chứng:** `connect my device` không tìm thấy thiết bị; và sau khi đã kết
nối, **không có gì tới thiết bị** (không Task Done / usage / ping) vì các lời gọi
mạng của hook bị chặn âm thầm.

**Cách sửa:** mở **System Settings → Privacy & Security → Local Network** và bật
cho ứng dụng đang chạy Claude Code (Terminal / iTerm / app Claude), rồi khởi động
lại ứng dụng đó.

Kiểm tra Python có tới được thiết bị không (thay bằng IP thiết bị của bạn):

```bash
python3 -c "import urllib.request as u; print(u.urlopen('http://192.168.1.50:5002/health', timeout=2).read())"
```

Trả về `{"status":"ok",...}` là đã thông. Nếu Python báo `No route to host` trong
khi `curl` tới cùng địa chỉ vẫn được thì đó là dấu hiệu quyền vẫn đang tắt.

## Trạng thái / việc còn lại

> **Các endpoint phía daemon `POST /claude-code/notify` và
> `POST /claude-code/usage` nay đã tồn tại.** Chúng nhận các push mô tả ở trên,
> **ghi log** payload, và trả về `{"ok":true}`. Việc **chưa** xong là bắc cầu các
> sự kiện đã log đó tới phản hồi thực tế của thiết bị — chưa có HAL bridge, nên các
> push chưa tạo ra đầu ra LED / màn hình tròn / giọng nói. Phần việc còn lại là
> render phía thiết bị (đấu nối các payload đã log qua HAL). Khám phá (mDNS) và cấu
> hình phía Mac của plugin vẫn hoạt động bình thường.
