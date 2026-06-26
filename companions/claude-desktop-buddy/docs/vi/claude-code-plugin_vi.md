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

## Phê duyệt bằng giọng nói (Claude Code reverse approval)

Khi Claude Code trên máy Mac sắp hiện **hộp thoại cấp quyền cho công cụ**, một hook
có thể hỏi thiết bị đã kết nối thay vì hiện hộp thoại: agent trên thiết bị đọc to
yêu cầu, người dùng trả lời **có/không bằng giọng nói**, và quyết định chảy ngược
về để Claude Code phê duyệt hoặc từ chối công cụ **mà không bao giờ hiện hộp
thoại**. Đây là **bản sao HTTP** của vòng phê duyệt qua BLE của Claude Desktop vốn
đã có (xem [`architecture_vi.md`](architecture_vi.md)).

Khả năng mới làm được điều này là một **hook `PermissionRequest`** của Claude Code.
Nó chỉ kích hoạt khi hộp thoại cấp quyền *sắp* hiện, chặn đồng bộ (tối đa đến
timeout 60 giây của nó), và trả về quyết định qua stdout dưới dạng JSON:

```json
{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}
```

(exit `0`, `"behavior"` là `"allow"` hoặc `"deny"`). Nó **không** kích hoạt ở chế
độ `-p` / headless, cũng không kích hoạt với các công cụ đã được cho phép sẵn bởi
các luật cấp quyền của bạn.

### Vòng đi-về (round-trip)

```
 Mac (Claude Code)                         Thiết bị (Pi / OrangePi)
 ┌──────────────────────┐                  ┌────────────────────────────────────┐
 │ công cụ cần cấp quyền │                  │  daemon claude-code-buddy :5002     │
 │        │             │  POST            │                                    │
 │        ▼             │  approval-request│  đăng ký chờ ──► báo hiệu thiết bị: │
 │ hook PermissionRequest│ ────────────────►│    • nháy LED (thoáng qua)         │
 │ (chặn ≤60s)          │  (long-poll)     │    • màn tròn "Approve <tool>?"     │
 │        ▲             │                  │    • sự kiện sensing → OS server    │
 │        │ {decision}  │ ◄────────────────│  chặn tới khi giải quyết / hết ttl │
 └────────┼─────────────┘                  │            ▲                       │
          │                                │  approve/deny (loopback) ◄─ agent  │
   không hộp thoại, chạy tiếp              └────────────────────────────────────┘
```

1. Claude Code → hook `PermissionRequest` `scripts/on-permission-request.py`
   (đăng ký trong `hooks/hooks.json` của plugin, matcher `*`, timeout 60 giây).
2. Hook POST tới daemon thiết bị
   `POST http://<device>:5002/claude-code/approval-request {id, tool, input}` và
   **chặn** (long-poll).
3. Daemon đăng ký yêu cầu đang chờ, báo hiệu thiết bị — nháy LED thoáng qua, màn
   hình tròn hiện **"Approve `<tool>`?"**, và phát một sự kiện sensing
   `type=claude_code_approval` tới OS server `/api/sensing/event` — rồi chặn cho
   tới khi yêu cầu được giải quyết hoặc hết TTL của long-poll.
4. Agent OpenClaw trên thiết bị nghe thấy sự kiện sensing (platform skill
   [`skills/claude-buddy/SKILL.md`](../../../../skills/claude-buddy/SKILL.md)) và hỏi người dùng; khi **"có"** nó gọi
   `POST 127.0.0.1:5002/claude-code/approve {id}`, khi **"không"**
   `POST 127.0.0.1:5002/claude-code/deny {id}`.
5. Daemon mở chặn long-poll → trả về `{"decision":"allow"|"deny"}` → hook in ra
   JSON quyết định `PermissionRequest` → Claude Code chạy tiếp với **không hộp
   thoại**.

### Các endpoint phê duyệt bằng giọng nói (`:5002`)

| Endpoint | Body | Hành vi |
|----------|------|---------|
| `POST /claude-code/approval-request` | `{id, tool, input, hint?}` | Long-poll; trả về `{"decision":"allow"｜"deny"｜"timeout"}`. `400` nếu thiếu `id`. |
| `POST /claude-code/approve` | `{id}` | Giải quyết yêu cầu đang chờ thành allow. **Chỉ loopback (`403` nếu không)** — vì nó quyết định liệu code có chạy trên máy Mac của người dùng hay không. `409` nếu `id` không rõ hoặc đã được giải quyết. |
| `POST /claude-code/deny` | `{id}` | Giống `approve` nhưng giải quyết thành deny (chỉ loopback, `403` / `409` như trên). |
| `GET /claude-code/pending` | — | `{"pending":[{id,tool,hint}]}` — những gì đang chờ một câu trả lời bằng giọng nói. |

### Cấu hình

- **Plugin** (`~/.config/claude-code-buddy.json`) — cờ mới `approval_enabled`
  (bool, mặc định **`false`**). Đây là tính năng **opt-in** vì nó thay đổi cách
  Claude Code hỏi bạn. Khi `false`, hook không làm gì và **hộp thoại gốc** sẽ hiện.
- **Daemon** (`config/buddy.json`) — `code_approval_ttl_sec` (mặc định **55**): một
  yêu cầu long-poll trong bao lâu trước khi trả về `"timeout"`.

### Cơ chế an toàn (fail-safe)

Hook **fail-open về hộp thoại gốc** và **không bao giờ tự động phê duyệt trên đường
lỗi**. Với bất kỳ trường hợp nào sau đây: tính năng bị tắt, không có thiết bị kết
nối, thiết bị không tới được, timeout, hoặc bất kỳ lỗi nào khác — hook in ra
**không gì cả** và exit `0`, nên Claude Code hiện hộp thoại cấp quyền **bình
thường**.

### Điều kiện tiên quyết / lưu ý

- **Local Network Privacy của macOS** phải cấp **Local Network** cho ứng dụng đang
  chạy Claude Code (`python3` của hook phải tới được IP thiết bị) — cùng cánh cổng
  với push đi ra (xem [bên dưới](#khắc-phục-sự-cố-local-network-trên-macos)).
- **Chế độ headless `-p`:** `PermissionRequest` không kích hoạt, nên phê duyệt bằng
  giọng nói nằm ngoài phạm vi ở đó.

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
