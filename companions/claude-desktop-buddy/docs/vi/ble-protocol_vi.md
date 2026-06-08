# Giao thức BLE

Thiết bị và Claude Desktop giao tiếp qua Bluetooth LE **Nordic UART Service
(NUS)**. Định dạng truyền là **JSON phân cách bằng newline** — mỗi message là một
đối tượng JSON đơn theo sau bởi `\n`. Tài liệu này là tham chiếu cho định dạng đó;
phần triển khai nằm trong `ble.go` (transport) và `protocol.go` (messages).

## Bố cục GATT

| Item | UUID | Direction | Flags |
|------|------|-----------|-------|
| Service (NUS) | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — | — |
| RX characteristic | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | Desktop → device | Write, Write-Without-Response |
| TX characteristic | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | device → Desktop | Notify, Read |

Thiết bị đóng vai trò **peripheral**: nó quảng bá UUID service NUS và local name
của nó (ví dụ `Claude-lamp-a1b2`). Claude Desktop là **central** thực hiện scan,
kết nối và subscribe các notification trên TX.

### Thời gian advertising

Backend Linux của tinygo để mặc định advertising ở 1.28 s của BlueZ, vốn quá chậm
so với cửa sổ scan của macOS. Khi khởi động, `tuneAdvIntervals()` ghi
`adv_min_interval=160` (100 ms) và `adv_max_interval=320` (200 ms) — đơn vị là
0.625 ms — vào mọi knob debugfs `/sys/kernel/debug/bluetooth/hci*`. Theo cơ chế
best-effort: nếu không có debugfs thì thiết bị quay về dùng mặc định của BlueZ.

## Framing

- **Inbound:** các byte từ RX characteristic được buffer lại và tách theo `\n`
  (`handleRX`). Một dòng chưa hoàn chỉnh vẫn nằm trong buffer cho đến khi newline
  kết thúc của nó đến trong một lần write sau đó. Buffer được reset khi disconnect.
- **Outbound:** `Send` thêm `\n` và ghi dòng đó lên TX theo **chunk 180 byte**
  (dưới mức MTU ~185 B được macOS thương lượng) để một ack thông thường vừa trong
  một notification.

### Cứu vãn mất gói (packet-loss salvage)

Claude Desktop ghi RX qua **Write-Without-Response**, vốn không có xác nhận ATT,
nên BlueZ có thể âm thầm rớt gói khi tải cao. `ParseOrSalvage` xử lý hậu quả đó:

1. Thử parse toàn bộ dòng. Nếu thành công thì xong (mất 0 byte).
2. Nếu không, quét tìm vị trí **cuối cùng** của một opener đã biết — `{"cmd":"`,
   `{"time":`, `{"total":`, `{"evt":"` — mà parse được, rồi dùng phần đuôi tính từ
   đó.
3. Nếu không có gì parse được, dòng đó bị bỏ và được phân loại trong log là
   `prefix-lost` (mất phần đầu), `truncated` (mất phần đuôi, không có `}` đóng),
   hoặc `mid-corruption` (một chunk bên trong mảng bị rớt).

Bất kỳ tổn thất framing nào cũng **hủy một folder transfer đang diễn ra**, vì
luồng byte của nó không còn đáng tin cậy nữa.

## Inbound messages (Desktop → device)

Parser dispatch dựa trên sự hiện diện của một trường discriminator, theo thứ tự
này: `cmd` → `time` → `evt` → `total`.

### Heartbeat

Gửi mỗi ~10 s và khi có thay đổi trạng thái. Điều khiển state machine. Được phân
biệt bởi việc có trường `total` (và không có `cmd`/`time`/`evt`).

```json
{
  "total": 3, "running": 1, "waiting": 0,
  "msg": "Editing handler.go", "entries": ["...", "..."],
  "tokens": 152340, "tokens_today": 482000,
  "prompt": { "id": "p-9f3", "tool": "Bash", "hint": "rm -rf build/" }
}
```

| Field | Ý nghĩa |
|-------|---------|
| `total` / `running` / `waiting` | số đếm session; `running > 0` → state `busy` |
| `msg` | văn bản trạng thái hiện tại (hiển thị trên monitor) |
| `entries` | các dòng hoạt động gần đây |
| `tokens` / `tokens_today` | mức sử dụng; khi `tokens` vượt một bội số của 50 000 → `celebrate` |
| `prompt` | **chỉ xuất hiện khi cần cấp quyền** → state `attention` |

`prompt` = `{ id, tool, hint }`. Thiết bị chỉ log heartbeat khi có gì đó đáng kể
thay đổi (running / waiting / msg / prompt đến hoặc được xóa); riêng số đếm token
không kích hoạt một dòng log.

### Event

Stream các turn chat và các sự kiện Desktop khác. Được phân biệt bởi `evt`.

```json
{ "evt": "turn", "role": "assistant", "content": [ {"type":"tool_use","name":"Read","input":{...}} ] }
{ "evt": "turn", "role": "user", "content": "fix the build" }
```

- `content` **hoặc** là một chuỗi thuần (user turns) **hoặc** là một mảng các
  content block của Anthropic (assistant turns + tool results).
- Các loại block được nhận biết: `text`, `thinking`, `tool_use` (`name`,`input`),
  `tool_result` (`tool_use_id`,`content`), `tool_reference` (`tool_name`).
- Thiết bị phát các event tới monitor bus của Lamp và thuật lại các block
  `thinking` / `tool_use` dưới dạng TTS. Không cần ack.

### Command

Điều khiển + folder-push. Được phân biệt bởi `cmd`.

```json
{ "cmd": "status" }
{ "cmd": "owner", "name": "Leo" }
{ "cmd": "name",  "name": "Lamp" }
{ "cmd": "unpair" }
```

Sub-protocol folder-push (stream một folder tới thiết bị):

| `cmd` | Fields | Action |
|-------|--------|--------|
| `char_begin` | `name`, `total` (bytes) | bắt đầu một transfer vào `chars/<name>/` |
| `file` | `path` (tương đối), `size` | mở một file mới |
| `chunk` | `d` (base64) | nối các byte đã decode vào file hiện tại |
| `file_end` | — | đóng file hiện tại |
| `char_end` | — | hoàn tất transfer |

Các path được sanitize: không path tuyệt đối, không traversal `..`, không có dấu
phân cách trong `name`. Mọi command đều được ack (xem bên dưới). Thư mục gốc đích
là `/opt/claude-desktop-buddy/chars`.

### TimeSync

```json
{ "time": [1717843200, -25200] }
```

`[epoch_seconds, utc_offset_seconds]`, gửi khi connect. Được log; không ack.

## Outbound messages (device → Desktop)

### Ack

Gửi cho mỗi `Command` nhận được.

```json
{ "ack": "chunk", "ok": true, "n": 4096 }
```

`n` là số byte, dùng cho `chunk` (số byte trong file hiện tại) và `file_end`.
`MakeAck` đặt `n:0`; `MakeAckN` mang theo số đếm.

### Status ack

Phản hồi cho `{ "cmd": "status" }` mang theo thông tin thiết bị (các key được viết
tắt để khớp với kỳ vọng của Claude Desktop):

```json
{
  "ack": "status", "ok": true,
  "data": {
    "name": "Claude-lamp-a1b2",
    "sec": false,
    "bat": { "pct": 100, "mV": 5000, "mA": 0, "usb": true },
    "sys": { "up": 3600, "heap": 0 },
    "stats": { "appr": 12, "deny": 3, "vel": 0, "nap": 0, "lvl": 0 }
  }
}
```

`sec` (link được mã hóa) hiện tại là `false` — xem ghi chú về bonding trong
[`architecture.md`](architecture_vi.md). `bat` được cố định (Pi luôn chạy USB).
`appr`/`deny` là bộ đếm số lần approve/deny trong toàn bộ vòng đời.

### Permission decision

Gửi khi agent approve hoặc deny một prompt (qua HTTP API):

```json
{ "cmd": "permission", "id": "p-9f3", "decision": "once" }
```

`decision` là `"once"` (approve) hoặc `"deny"`. `id` phải khớp với `id` của prompt
từ heartbeat.

## Tóm tắt message

| Direction | Type | Discriminator | Ack? |
|-----------|------|---------------|------|
| Desktop → device | Heartbeat | `total` | no |
| Desktop → device | Event | `evt` | no |
| Desktop → device | Command | `cmd` | **yes** |
| Desktop → device | TimeSync | `time` | no |
| device → Desktop | Ack / Status ack | `ack` | — |
| device → Desktop | Permission | `cmd:"permission"` | — |
