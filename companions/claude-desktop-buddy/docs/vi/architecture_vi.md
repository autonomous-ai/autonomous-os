# Kiến trúc

Claude Desktop Buddy là một binary Go duy nhất (`buddy-plugin`) chạy trên thiết
bị, làm cầu nối giữa **Claude Desktop trên máy Mac của người dùng** với runtime
phần cứng của đèn. Tài liệu này mô tả các thành phần của nó, cách dữ liệu chảy qua
chúng, và các giao kèo (contract) mà nó nói chuyện ở mỗi phía.

Về định dạng message BLE trên đường truyền, xem [`ble-protocol_vi.md`](ble-protocol_vi.md).
Về build, deploy, cấu hình và pairing, xem [`setup_vi.md`](setup_vi.md).

## Hai phía

```
   Mac                                    Device (Pi / OrangePi)
 ┌───────────────────┐               ┌───────────────────────────────────────┐
 │  Claude Desktop    │   BLE / NUS   │  buddy-plugin                          │
 │  "Hardware Buddy"  │ ◄───────────► │                                        │
 └───────────────────┘  notify / write│  ble.go ─► state.go ─► bridge.go       │
                                       │     │                      │  │  │     │
                                       │     ▼                      ▼  ▼  ▼     │
                                       │  httpserver.go        LeLamp / Lamp    │
                                       │  :5002 (OpenClaw)     HTTP (LED, eyes, │
                                       │                       TTS, monitor)    │
                                       └───────────────────────────────────────┘
```

- **Hướng Bắc (BLE):** Claude Desktop là BLE *central*; thiết bị là *peripheral*
  quảng bá (advertise) một Nordic UART Service. Desktop stream các heartbeat, sự
  kiện chat, prompt xin quyền và dữ liệu folder; thiết bị phản hồi bằng các ack và
  quyết định cấp/từ chối quyền.
- **Hướng Nam (HTTP):** hành vi của thiết bị được tạo ra bằng cách gọi hai HTTP
  server cục bộ — **LeLamp** (runtime phần cứng, mặc định `:5001`) cho LED / màn
  hình / cảm xúc / TTS, và **Lamp** (Go API, mặc định `:5000`) cho monitor và các
  bus cảm biến (sensing).
- **Hướng Đông (HTTP):** bản thân Buddy phục vụ một HTTP API nhỏ trên `:5002` để
  agent trên thiết bị (OpenClaw skill) gọi đọc trạng thái và chấp thuận/từ chối các
  prompt.

## Các thành phần (theo file)

| File | Trách nhiệm |
|------|----------------|
| `main.go` | Điểm vào của process. Nạp config, đăng ký BlueZ pairing agent, nối state machine → bridge + narrator, khởi động BLE server, ticker cho transient state, và HTTP server. Sở hữu bộ dispatcher message BLE `handleBLEMessage`. |
| `ble.go` | Nordic UART GATT server (qua một fork vendored của `tinygo.org/x/bluetooth`). Advertising, đóng khung theo dòng (line framing), salvage, ghi notify theo chunk, tinh chỉnh advertising-interval. |
| `protocol.go` | Tất cả các kiểu trên đường truyền (`Heartbeat`, `TimeSync`, `Event`, `Command`, `Ack`, `PermissionDecision`), bộ parser/salvager, và các builder ack/permission. |
| `state.go` | `StateMachine` — suy ra một `BuddyState` từ heartbeat, theo dõi prompt đang chờ cùng các bộ đếm cấp/từ chối trọn đời (lifetime), và quản lý các transient state. |
| `bridge.go` | Ánh xạ mỗi thay đổi trạng thái thành các lời gọi HTTP cụ thể tới LeLamp + Lamp (LED, màn hình, cảm xúc, TTS, sự kiện monitor/sensing). |
| `httpserver.go` | API `:5002`: `GET /status`, `GET /health`, `POST /approve`, `POST /deny`. |
| `agent.go` | BlueZ `org.bluez.Agent1` (DisplayOnly) để LE pairing có thể hoàn tất; ghi passkey ra journal. |
| `transfer.go` | Bộ nhận folder-push — stream file từ Desktop tới `/opt/claude-desktop-buddy/chars` kèm các bảo vệ chống path-traversal. |
| `narrator.go` + `i18n.go` | Các thông báo TTS ngắn về hoạt động của Claude, dedupe theo từng turn, được bản địa hóa (EN/VI). |
| `stats.go` | Lưu bền các bộ đếm approved/denied trọn đời vào `/var/lib/claude-desktop-buddy/stats.json`. |
| `config/buddy.json` | Config lúc chạy (xem [`setup_vi.md`](setup_vi.md)). |
| `skill/SKILL.md` | OpenClaw skill biến các lượt cấp quyền thành tương tác bằng giọng nói. |
| `third_party/bluetooth/` | Fork cục bộ của `tinygo.org/x/bluetooth` (xem bên dưới). |

## Luồng dữ liệu

### Chiều vào (Desktop → device)

1. **Lệnh ghi BLE** đáp lên RX characteristic. `ble.go:handleRX` nối các byte vào
   một buffer và tách theo `\n`; mỗi dòng hoàn chỉnh được đẩy vào `msgCh`.
2. Một **goroutine processor** duy nhất rút cạn `msgCh` và gọi `handleBLEMessage`
   (trong `main.go`) từng dòng một, nhờ vậy trạng thái transfer dùng chung không bị
   race.
3. `ParseOrSalvage` (`protocol.go`) giải mã dòng thành message có kiểu, hoặc khôi
   phục phần đuôi của một message bị hỏng (BLE write-without-response không có ACK,
   nên BlueZ âm thầm rớt packet khi tải cao).
4. Dispatch theo kiểu:
   - **`Heartbeat`** → `StateMachine.HandleHeartbeat` → có thể chuyển trạng thái →
     kích hoạt `OnStateChange` → `bridge` vẽ lại LED/màn hình + `narrator` thông báo.
   - **`Event`** (`evt:"turn"`) → `bridge.OnEvent` đẩy lên monitor bus của Lamp; các
     lượt của assistant được soi theo từng block để các block `thinking`/`tool_use`
     trở thành lời thuật TTS.
   - **`Command`** (`status`, `owner`, `name`, `unpair`, folder-push) → được xử lý và
     **ack** qua TX characteristic.
   - **`TimeSync`** → ghi log (không cần ack).

### Chiều ra (device → Desktop)

`ble.go:Send` ghi JSON kết thúc bằng newline vào TX characteristic, chia chunk ở
180 byte (dưới MTU mà macOS đã thương lượng). Hai thứ đi ra theo đường này:
- **Ack** cho mỗi `Command` nhận được (`protocol.go:MakeAck` / `MakeAckN` /
  `MakeStatusAck`).
- **Quyết định quyền** (`MakePermission`) khi agent cấp/từ chối.

### Vòng round-trip cấp quyền

```
Desktop heartbeat carries `prompt`  ─►  state = attention, pending prompt stored
        │                                         │
        │                                bridge.postSensingEvent
        │                                  → Lamp /api/sensing/event
        │                                    type=buddy_approval  ─► agent hears it,
        │                                                            asks the user
        │                                                                  │
   GET /status (skill polls) ◄────────────────────────────────────────────┘
        │  pending_prompt {id, tool, hint}
        ▼
   user says yes/no  ─►  POST /approve|/deny {id}
        │
        ▼
   httpserver validates id == pending.id
        │
        ▼
   ble.Send(MakePermission(id, "once"|"deny"))  ─►  Desktop applies the decision
        │
        ▼
   StateMachine.Approved()/Denied()  ─►  counters++ , stats persisted
```

`POST /approve` gửi quyết định `"once"`; `POST /deny` gửi `"deny"`. Cả hai trả về
`409` nếu không có prompt đang chờ hoặc `id` không khớp với prompt hiện tại.

## State machine

Các trạng thái (`state.go`): `sleep`, `idle`, `busy`, `attention`, `heart`, `celebrate`.

| Trạng thái | Khi nào | Biểu hiện trên đèn (`bridge.go`) |
|-------|------|-------------------------------|
| `sleep` | BLE ngắt kết nối | khôi phục LED của người dùng, mắt buồn ngủ |
| `idle` | đã kết nối, không có gì đang chạy | khôi phục LED của người dùng, chế độ eyes |
| `busy` | heartbeat `running > 0` | nhịp đập (màu thương hiệu Claude), info: tokens hôm nay + sessions |
| `attention` | heartbeat mang theo một `prompt` | nhấp nháy, info: "Approve `<tool>`?", phát sự kiện sensing `buddy_approval` |
| `heart` | quyền được cấp trong vòng 5 s kể từ prompt | màu solid, mắt vui (3 s, transient) |
| `celebrate` | vượt mốc token (mỗi 50 000) | cầu vồng, mắt hào hứng (3 s, transient) |

Các transient state (`heart`, `celebrate`) giữ trong 3 s và không bị các heartbeat
đến ghi đè; một ticker 500 ms (`CheckTransientExpiry`) suy ra lại trạng thái thật
khi chúng hết hạn. Kết nối/ngắt kết nối được phát hiện từ heartbeat đầu tiên và từ
BlueZ connect handler.

## Bridge (phía Nam)

Mọi lệnh ghi LED của Buddy đều được đánh dấu `transient: true`: chúng vẽ dải LED mà
không ghi đè trạng thái LED đã lưu của người dùng. Khi quay về `idle`/`sleep`,
`ledRestore()` yêu cầu LeLamp vẽ lại đúng những gì người dùng đã đặt, nên Buddy
không bao giờ chiếm dải LED vĩnh viễn. Màu nhấn là màu app Claude **`#C15F3C`**
(`claudeBrand`).

| Lời gọi | Endpoint | Mục đích |
|------|----------|---------|
| `ledSolid` / `ledEffect` / `ledRestore` / `ledOff` | LeLamp `/led/*` | tín hiệu LED theo trạng thái (đều transient) |
| `displayInfo` / `displayEyes` / `displayEyesMode` | LeLamp `/display/*` | text trên màn hình tròn + mắt |
| `expressEmotion` | LeLamp `/emotion` | phối hợp LED+servo (ví dụ "happy" khi một lượt kết thúc) |
| `speakTTS` (đã cache) / `prerenderTTS` | LeLamp `/voice/speak` | TTS thuật lại (và làm nóng cache lúc khởi động) |
| `postBuddyState` | Lamp `/api/monitor/event` (`type=buddy_state`) | hiển thị trạng thái lên monitor |
| `postSensingEvent` | Lamp `/api/sensing/event` (`type=buddy_approval`) | bơm yêu cầu cấp quyền vào pipeline sensing |
| `OnEvent` | Lamp `/api/monitor/event` (`type=buddy_event`) | hiển thị các lượt chat cho các use case khác |

Mọi lời gọi của bridge đều theo kiểu fire-and-forget với timeout 5 s; các phản hồi
`409` (đang bận phát nhạc) / `503` (TTS chưa sẵn sàng) của chính LeLamp được bỏ qua
ở đây.

## Thuật lại (UC-9)

`narrator.go` phát các cụm trạng thái ngắn dưới dạng TTS. Chính sách:
- **Dedupe theo từng turn** — mỗi hạng mục phát tối đa một lần mỗi turn (một turn bắt
  đầu khi có message mới của người dùng hoặc khi chuyển idle→busy), nên nhiều block
  `thinking` không lặp lại cùng một cụm.
- **Ánh xạ tool** — `i18n.go:toolToCategory` ánh xạ tên tool của Claude Code thành
  một cụm (`Read`→"đang đọc một file", `Bash`→"đang chạy một lệnh shell", `mcp__*`→
  "đang gọi MCP", không rõ→cụm chung "đang dùng một tool" và bỏ tên thô đi — tên tool
  đọc lên qua TTS không hay).
- **Warmup** — lúc khởi động, mọi cụm được gửi tới LeLamp với `prerender:true` để
  thông báo thật đầu tiên phát từ cache TTS trên đĩa thay vì phải chờ một round-trip
  tới provider.
- **Ngôn ngữ** — `narration_lang` trong config (`en`/`vi`); giá trị không rõ sẽ rơi
  về tiếng Anh.

## Lưu bền (Persistence)

- **Stats** — các bộ đếm approved/denied trọn đời nằm tại
  `/var/lib/claude-desktop-buddy/stats.json` (đặt dưới `/var/lib` để chúng sống sót
  qua các lần nâng cấp gói vốn xóa sạch `/opt`, và tách khỏi config để việc reset
  config không làm chúng về 0). Được khôi phục lúc boot nên `/status` đúng ngay sau
  khi restart.
- **Folder đã push** — `/opt/claude-desktop-buddy/chars/<name>/...`.

## Fork BLE tinygo

`go.mod` thay thế `tinygo.org/x/bluetooth` bằng `third_party/bluetooth` cục bộ. Lớp
GATT Linux của upstream chỉ ánh xạ sáu cờ characteristic cơ bản; fork bổ sung
secure-read / secure-write để có thể diễn đạt yêu cầu bonding của Hardware Buddy.
(Hiện các cờ secure-only bị *bỏ* lúc chạy — xem ghi chú trong `ble.go` — vì client
Mac kết nối mà không tự kích hoạt SMP; liên kết hiện chưa mã hóa và `status.sec`
được báo là `false`.)

## Ghi chú / điểm dễ vấp

- **`buddy.json` `led_mapping` bị bỏ qua.** Struct `Config` trong `main.go` không có
  trường cho nó; hành vi LED được hardcode trong `bridge.go`. Khối này là di sản cũ
  (legacy).
- **`approval_timeout_sec` được parse nhưng không dùng.** Cửa sổ "heart" 5 s được
  hardcode trong `state.go`; hiện không có timeout cấp quyền phía server.
- **Tên thiết bị** — `device_name` có thể chứa `{MAC}`, được mở rộng lúc khởi động từ
  `/api/system/network` của Lamp (4 hex cuối của serial Pi / MAC eth0), khớp với
  hostname mDNS `lamp-xxxx.local`. Bị cắt còn 4 ký tự để vừa quảng bá BLE 31 byte.
