# Claude Desktop Buddy — Spec tích hợp

> Biến đèn Lamp thành Hardware Buddy của Claude Desktop. Chạy như plugin
> Go độc lập trên Pi, bridge trạng thái BLE của Claude vào hệ HAL
> (LED/display/audio) và Lamp (OpenClaw/sensing) sẵn có.

**Nguồn**: [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy) (firmware ESP32 reference + protocol REFERENCE.md)
**Trạng thái**: Implementation — Phase 1, 2, 3 đã ship (2026-05-11)
**Phần cứng**: Raspberry Pi 4 / Orange Pi 4 Pro (chip BT AIC8820)

---

## 1. Cái gì & Tại sao

Claude Desktop ("Claude for macOS/Windows") expose 1 BLE API trong
Developer Mode để hardware companion kết nối qua Nordic UART Service.
Reference của Anthropic là ESP32 desk pet — LCD nhỏ, 2 nút, không có
brain. Lamp implement cùng wire protocol nhưng là **smart buddy**: đèn
đầy đủ với camera, mic, speaker, LED ring, servo, display, và brain
agentic OpenClaw.

Lamp có thể phản ánh state Claude lên LED, voice-approve tool call rảnh
tay, stream chat turns ra display/TTS, và feed context presence ngược lại.

### Use case

| # | Use case | Trạng thái | Mô tả |
|---|----------|------------|-------|
| UC-1 | **Ambient state** | [x] xong | LED ring phản ánh state Claude (sleep/idle/busy/attention/heart/celebrate). Lamp lamp hiện không có LCD/display, chỉ điều khiển LED. |
| UC-2 | **Voice approval** | [x] xong | Prompt tool-call → Lamp đọc qua skill OpenClaw → user nói approve/deny rảnh tay. |
| UC-3 | **Thống kê hoạt động qua HTTP** | [x] xong | Buddy track token count, sessions chạy, approval stats; expose qua `GET /status` cho consumer local. (Chưa có display trên lamp.) |
| UC-4 | **Fan-out chat turn** | [x] xong | Mọi `evt:"turn"` (user/assistant/tool blocks) được forward lên Lamp monitor bus dạng `buddy_event` — sẵn cho TTS, transcript memory, dashboard. |
| UC-5 | **Nhận character pack** | [x] xong | Desktop drag GIF folder vào panel → stream qua BLE → lưu vào `/opt/claude-desktop-buddy/chars/<name>/`. |
| UC-9 | **TTS narration trạng thái** | [x] xong | Thông báo ngắn khi state đổi ("Claude đã kết nối" / "Claude bắt đầu" / "Claude xong rồi" / "Claude đã ngắt kết nối") và cho mỗi block `tool_use` / `thinking` ("Claude đang sửa file", "Claude đang tìm web", …). Multi-lang (`vi` / `en` / `zh`) trong `i18n.go`, throttle 1 lần/category/turn. Gọi HAL `/voice/speak` với `cached: true` để phrase set bounded hit TTS cache on-disk; `Narrator.Warmup` chạy mọi phrase với `prerender: true` 8s sau khởi động nên lần đầu cũng phát từ cache. Transition busy→idle gọi thêm `/emotion {happy,0.7}` để HAL phối hợp LED + servo "thở ra" giữa các turn. Tool lạ fallback sang câu generic không kèm tên — tên tool Claude Code (CamelCase, `mcp__*`) đọc qua TTS không thành tiếng. |
| UC-8 | **Đọc reply Claude qua TTS** | [ ] tiếp theo | Lamp subscribe `buddy_event`, filter `role=assistant` + text block, strip markdown, đẩy text qua HAL TTS để user nghe thay vì nhìn màn Mac. Respect presence (skip khi user vắng), busy state của voice pipeline, ưu tiên agent emotion. |
| UC-6 | **Presence feedback** | [ ] tương lai | Presence Lamp (camera/PIR) → Desktop. Cần mở rộng protocol. |
| UC-7 | **OpenClaw biết transcript** | [ ] tương lai | OpenClaw đọc history chat khi user hỏi qua voice. |

---

## 2. Kiến trúc

```
┌──────────────────┐       BLE (Nordic UART)        ┌──────────────────────────────┐
│  Claude Desktop  │ ◄───────────────────────────►  │          Lamp (Pi)           │
│  (Mac / Windows) │                                │                              │
│  Developer →     │   Heartbeat (msg/running/      │  ┌────────────────────────┐  │
│  Hardware Buddy  │   tokens/prompt), Event        │  │   buddy-plugin         │  │
│                  │   (turn/content blocks),       │  │   (Go, :5002)          │  │
│                  │   Command, TimeSync            │  │                        │  │
│                  │                                │  │   ble.go + agent.go    │  │
│                  │   Ack, PermissionDecision      │  │   protocol.go          │  │
│                  │                                │  │   state.go             │  │
│                  │                                │  │   bridge.go            │  │
│                  │                                │  │   httpapi/             │  │
│                  │                                │  │   transfer.go          │  │
│                  │                                │  └──┬──────────┬──────────┘  │
│                  │                                │     │ HTTP     │ HTTP        │
│                  │                                │     ▼          ▼             │
│                  │                                │  ┌─────────┐ ┌──────────┐    │
│                  │                                │  │  Lamp   │ │ HAL   │    │
│                  │                                │  │ :5000   │ │ :5001    │    │
│                  │                                │  │ OpenClaw│ │ LED ring │    │
│                  │                                │  │ sensing │ │ + TTS    │    │
│                  │                                │  │ monitor │ │ (no LCD) │    │
│                  │                                │  └─────────┘ └──────────┘    │
└──────────────────┘                                └──────────────────────────────┘
```

### Layout plugin

```
claude-desktop-buddy/
├── main.go              Entrypoint mỏng: flags + logging, rồi gọi buddy.Run()
├── buddy/               Logic daemon (package buddy)
│   ├── app.go           Orchestration Run(), load config, dispatch message BLE
│   ├── ble.go           BLE peripheral (GATT server, advertising, tune interval qua debugfs)
│   ├── agent.go         BlueZ DisplayOnly pairing agent (đã register nhưng chưa dùng — §5)
│   ├── protocol.go      Wire types: Heartbeat, TimeSync, Event, Command, Ack, PermissionDecision
│   ├── state.go         6-state machine (sleep/idle/busy/attention/heart/celebrate)
│   ├── bridge.go        HTTP outbound tới HAL (:5001) + Lamp (:5000)
│   ├── transfer.go      Nhận folder character-pack push (lưu vào chars/)
│   ├── narrator.go,i18n.go  TTS narration dedupe theo từng turn (EN/VI)
│   ├── stats.go         Counter approve/deny lifetime (lưu dưới /var/lib)
│   └── approval_service.go, status_provider.go, activity_sink.go   Impl của các port httpapi
├── httpapi/             Tầng delivery HTTP API :5002 (package httpapi)
│   ├── server.go        Server + registry routes() + JSON helpers
│   ├── ports.go         Interface: StatusProvider, ApprovalService, ActivitySink
│   ├── status.go        GET /status, GET /health
│   ├── claudedesktop.go POST /claude-desktop/approve, /deny
│   └── claudecode.go    POST /claude-code/notify, /usage (plugin Claude Code push)
├── claude-code-buddy/   Plugin Claude Code (Python) — push tới /claude-code/* (xem README riêng)
├── skill/SKILL.md       Skill OpenClaw cho voice approval flow
├── config/buddy.json    Template config (runtime đọc /root/config/buddy.json)
├── third_party/bluetooth/  Vendor tinygo bluetooth v0.14.0 + patch secure-* flag
├── go.mod               Go module riêng với replace tinygo.org/x/bluetooth => ./third_party/...
└── VERSION_BUDDY        Version stamp text, inject lúc build
```

### Process model

`buddy-plugin` là **systemd service độc lập** (`claude-desktop-buddy.service`),
tách khỏi binary Lamp chính. Restart độc lập; không link vào process
Lamp. Gọi Lamp và HAL qua HTTP local.

```
Layout runtime trên Pi:
  /opt/claude-desktop-buddy/buddy-plugin   — binary
  /opt/claude-desktop-buddy/VERSION_BUDDY  — version stamp
  /opt/claude-desktop-buddy/chars/         — character pack nhận về
  /root/config/buddy.json                  — config runtime (tạo 1 lần)
  /etc/systemd/system/claude-desktop-buddy.service   — service unit
  /var/log/claude-desktop-buddy.log                  — log rotate (2MB × 10)
```

---

## 3. Discovery + pairing — thực tế đang chạy

Spec Anthropic đề xuất LE Secure Connections bonding với DisplayOnly IO
capability. Thực tế client macOS hiện tại của Claude Desktop establish
LE connection plain mà không auto-trigger SMP — nên characteristics
encrypted-only không truy cập được và panel hiện "No response". Vì vậy
mình **expose characteristics NUS không cần encryption flag** và
connection chạy unbonded. Vendor fork thêm `secure-read`/`secure-write`
flags vẫn giữ trong tree, `agent.go` vẫn register DisplayOnly agent —
sẵn để bật lại khi nào Desktop side drive SMP.

### Filter discovery (Hardware Buddy panel)

Picker filter theo:

1. Device name bắt đầu bằng **`Claude`**
2. Advertise **Nordic UART Service UUID** (`6e400001-…`)

Mọi thiết bị khác bị ẩn trong picker, kể cả khi macOS Bluetooth Settings
thấy được.

### Pairing flow (phía Pi)

1. Config runtime BlueZ (phải có sẵn — `setup-claude-desktop-buddy.sh`
   set; fallback thủ công: `btmgmt -i 0 power off; bredr on; le on;
   connectable on; pairable on; discoverable on; power on` +
   `bluetoothctl discoverable-timeout 0`).
2. buddy-plugin start → đọc `/root/config/buddy.json` → resolve device
   name qua `resolveDeviceName()`:
   - Đọc `device_name` từ config (default `Claude-{deviceid}`).
   - Nếu có `{deviceid}`, fetch `device_id` từ Lamp
     `GET http://127.0.0.1:5000/api/system/info` (retry 15 × 2s).
   - `shortDeviceID()` giữ segment cuối sau dash, trim 4 ký tự
     (`lamp-004` → `004`) — để name + Nordic UART UUID cùng fit trong
     31-byte primary advertisement.
   - Fallback `Claude-unknown` nếu lamp chưa setup qua
     `/api/device/setup`.
3. `registerBluezAgent()` export `org.bluez.Agent1` với capability
   `DisplayOnly` lên system D-Bus, rồi gọi `RegisterAgent` +
   `RequestDefaultAgent` trên `org.bluez.AgentManager1`. Agent hiện chỉ
   log event `PAIRING PASSKEY` khi BlueZ hỏi — sẵn dùng nếu SMP được
   engage sau này.
4. `tuneAdvIntervals()` ghi `160` và `320` vào
   `/sys/kernel/debug/bluetooth/hci*/adv_{min,max}_interval` (đơn vị
   0.625 ms, tức 100–200 ms). Không có bước này thì BlueZ default
   1.28 s — quá thưa cho scan window ngắn của macOS.
5. tinygo register GATT service + advertisement. BlueZ đóng gói service
   UUID vào primary advertisement (18 byte) và local name vào scan
   response (~10 byte). macOS active scan, merge cả 2, hiện device
   trong picker.

### Pairing flow (phía Mac)

1. User: **Help → Troubleshooting → Enable Developer Mode** (1 lần).
2. **Developer → Open Hardware Buddy…** → bấm **Connect** → chọn
   `Claude-XXX` trong scan results.
3. Hardware Buddy mở LE GATT connection. macOS **không** initiate SMP
   vì characteristics mình không yêu cầu encryption.
4. Desktop gửi ngay `{"cmd":"owner","name":"…"}`, 1 TimeSync, rồi poll
   `{"cmd":"status"}` ~mỗi 2 giây.
5. Khi agentic turn bắt đầu, `Heartbeat` snapshot và `Event` chat turn
   bắt đầu chảy.

### Auto-reconnect

Mac cache device theo BD address. Lần buddy start tiếp theo tự reconnect
không cần user action. **Đổi tên device trên Pi không refresh tên cache
phía Mac** — Mac giữ tên cũ cho đến khi user explicit forget device
trong System Settings → Bluetooth, hoặc chạy `sudo pkill bluetoothd`.
Cache cứng đầu thì nuke plist (`sudo rm
/Library/Preferences/com.apple.Bluetooth*.plist` + pkill).

### Unpair

`{"cmd":"unpair"}` từ Desktop → buddy abort folder transfer đang chạy,
quay về advertising. Bond data phía BlueZ không cần xóa vì không có bond.

---

## 4. BLE wire protocol

### Transport

| Thuộc tính | Giá trị |
|------------|---------|
| Service UUID | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| RX (Desktop → Device, write + write-without-response) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| TX (Device → Desktop, notify + read) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| Wire format | UTF-8 JSON, mỗi object 1 dòng kết thúc `\n` |
| Tên device | Phải bắt đầu bằng `Claude` |
| Advertising interval | 100–200 ms (tune qua debugfs) |
| Encryption | Hiện không có; vendor fork sẵn sàng bật lại qua `secure-*` flags |

### Message: Desktop → Device

#### `Heartbeat` — snapshot state định kỳ

Gửi ~mỗi 1 s khi active, ~10 s khi idle. Parser dùng presence của
field `total` để discriminate.

```json
{
  "total": 3,
  "running": 1,
  "waiting": 0,
  "msg": "Editing main.go",
  "entries": ["Latest message", "Previous message"],
  "tokens": 52340,
  "tokens_today": 8200,
  "prompt": { "id": "req_abc123", "tool": "Edit", "hint": "server.go:10-20" }
}
```

Buddy throttle log: dòng `[ble] heartbeat …` chỉ emit khi `running`,
`waiting`, `msg`, hoặc `prompt` (presence/id) đổi — token đếm drift mỗi
ping, log đầy đủ sẽ spam journal.

#### `TimeSync` — clock + timezone

Gửi 1 lần khi connect (presence field `time`).

```json
{ "time": [1713600000, 25200] }
```

#### `Event` — stream chat turn

Gửi cho từng turn hội thoại (user input, assistant reply, tool use,
tool result). Presence field `evt`. `content` hoặc là string thuần
(user turn) hoặc array các typed content block (assistant + tool flow).

```json
{
  "evt": "turn",
  "role": "user",
  "content": "thử tìm giá BTC hôm nay đi"
}
```

```json
{
  "evt": "turn",
  "role": "assistant",
  "content": [
    {"type": "thinking", "thinking": "", "signature": "…base64…"},
    {"type": "tool_use", "id": "toolu_…", "name": "WebSearch",
     "input": {"query": "Bitcoin price today"}}
  ]
}
```

`formatContentBlock()` render mỗi block thành 1 dòng log tag:
`[thinking: …]`, `[tool_use <name>(<input>)]`, `[tool_result <id>: …]`,
`[tool_ref: <name>]`. Mỗi event fan-out lên Lamp qua
`bridge.OnEvent` với `type=buddy_event` trên monitor bus.

#### `Command` — control + folder push

Presence field `cmd`. Buddy ack mọi command.

| `cmd` | Payload | Hiệu lực |
|---|---|---|
| `status` | `{}` | Buddy trả `Ack` kèm battery/uptime/approval counts. |
| `owner` | `{"name": "Leo"}` | Ghi tên user Mac (cho log). |
| `name` | `{"name": "…"}` | Đổi tên device (cosmetic). |
| `unpair` | `{}` | Quay về advertising. |
| `char_begin` | `{"name":"bufo","total":1500000}` | Start folder transfer character pack. |
| `file` | `{"path":"sleep.gif","size":12345}` | Mở file mới trong transfer hiện hành. |
| `chunk` | `{"d":"<base64>"}` | Append byte đã decode vào file đang mở. |
| `file_end` | `{}` | Đóng file hiện hành. |
| `char_end` | `{}` | Kết thúc transfer; folder lưu tại `chars/<name>/`. |

### Message: Device → Desktop

#### `Ack` — response cho command

```json
{ "ack": "owner", "ok": true, "n": 0 }
```

Status ack có payload `data`:

```json
{
  "ack": "status",
  "ok": true,
  "data": {
    "name": "Claude-004",
    "sec": false,
    "bat": { "pct": 100, "mV": 5000, "mA": 0, "usb": true },
    "sys": { "up": 86400, "heap": 0 },
    "stats": { "appr": 3, "deny": 0, "vel": 0, "nap": 0, "lvl": 0 }
  }
}
```

`sec` hiện hardcode `false` (chưa bond). Sẽ set `true` khi bật
encryption.

#### `PermissionDecision` — approve/deny

Gửi để phản hồi `prompt` trong heartbeat. `id` echo lại prompt id.

```json
{ "cmd": "permission", "id": "req_abc123", "decision": "once" }
```

| Decision | Hiệu lực |
|---|---|
| `"once"` | Approve tool call đang pending. |
| `"deny"` | Reject. |

### Salvage khi packet loss write-without-response

Claude Desktop stream chunk qua BLE write-without-response — không có
ATT confirmation. Khi BlueZ drop chunk im lặng, dòng kế tiếp mình
extract sẽ corrupted brackets. `ParseOrSalvage` tìm opener JSON gần
nhất (`{"cmd":"`, `{"time":`, `{"total":`, `{"evt":"`) trong buffer rồi
thử lại. Nếu vẫn không parse được, drop dòng kèm 1 trong 3 category log:

- `dropped N-byte BLE message (prefix-lost): …` — đầu dòng mất; không
  cứu được.
- `dropped N-byte BLE message (truncated): …` — bắt đầu JSON nhưng
  không có `}` đóng.
- `dropped N-byte BLE message (mid-corruption): …` — brackets khớp
  nhưng chunk giữa `entries`/`content` biến mất.

Cả 3 đều abort folder transfer đang chạy vì framing mất.

---

## 5. Trạng thái encryption (deferred)

Theo spec Anthropic, characteristics NUS nên yêu cầu LE Secure
Connections bonding với device expose IO capability DisplayOnly, và
report `sec: true` khi đã bond. Vendor fork tại `third_party/bluetooth/`
thêm BlueZ flags `secure-read` và `secure-write` vào enum
`CharacteristicPermissions` 6-bit của `tinygo.org/x/bluetooth` để mình
làm được điều này bằng Go.

Blocker: client macOS Hardware Buddy hiện tại connect mà không gửi
request pairing SMP, nên khi characteristics mình mark secure-only
BlueZ từ chối mọi GATT operation và panel mãi hiện "No response". Để
integration end-to-end hoạt động hôm nay mình ship characteristics với
flags plain `write` / `write-without-response` / `notify` / `read` và
report `sec: false`. Plumbing agent + flag vẫn ready để flip lại
encrypted-only khi Anthropic bật auto-pairing hoặc khi tìm ra cơ chế
"Pair" explicit để drive.

---

## 6. State machine

```
                    ┌─────────────┐
   BLE off ────────►│    sleep    │◄──────── BLE disconnect
                    │ LED: off    │
                    └──────┬──────┘
                           │ connect
                           ▼
                    ┌─────────────┐
   running == 0    │    idle     │
   ───────────────►│ ambient-led │
                    └──────┬──────┘
                           │ running > 0
                           ▼
                    ┌─────────────┐
   waiting == 0    │    busy     │
   ───────────────►│ LED: pulse  │
                    └──────┬──────┘
                           │ heartbeat.prompt != null
                           ▼
                    ┌─────────────┐
                    │  attention  │
                    │ LED: blink  │
                    │ + sensing   │
                    └──────┬──────┘
                           │ /claude-desktop/approve hoặc /deny
                           ▼
                    ┌─────────────┐
                    │   heart     │  approve <5s
                    │ LED: warm   │  (3s, rồi suy lại)
                    └─────────────┘

      Tokens vượt mốc 50K:
                    ┌─────────────┐
                    │  celebrate  │  rainbow burst
                    │ LED: rainbow│  (3s, rồi suy lại)
                    └─────────────┘
```

### Quy tắc suy state

```
disconnect              → sleep
heartbeat.prompt != nil → attention
heartbeat.running > 0   → busy
khác                    → idle

approve trong vòng 5s từ khi prompt đến → heart   (overlay 3s)
tokens / 50_000 tăng                    → celebrate (overlay 3s)
```

Overlay transient (`heart`, `celebrate`) khóa state 3 s; ticker expiry
(`CheckTransientExpiry`, 500 ms) tự suy lại từ heartbeat cuối khi hết.

---

## 7. State → bridge HAL + Lamp

`Bridge.OnStateChange` được wire làm callback transition của state
machine. Mỗi transition fire:

| State | HAL LED call | Lamp monitor event |
|---|---|---|
| `sleep` | `POST /led/off` | `buddy_state` |
| `idle` | (không gọi — ambient quản LED) | `buddy_state` |
| `busy` | `/led/effect {pulse,[0,100,255],0.8}` | `buddy_state` |
| `attention` | `/led/effect {blink,[255,80,0],1.5}` | `buddy_state` + **`buddy_approval` sensing event** |
| `heart` | `/led/solid {[255,200,100]}` | `buddy_state` |
| `celebrate` | `/led/effect {rainbow,*,2.0,3000ms}` | `buddy_state` |

> Lamp lamp hiện không có LCD/eye display; code `bridge.go` vẫn cố gọi
> `/display/info`, `/display/eyes`, `/display/eyes-mode` qua HAL,
> nhưng đây là no-op trên hardware không màn. Hoặc xóa các nhánh này
> khi không-display là permanent, hoặc bổ sung display peripheral.

Thêm vào đó, mọi `Event` (chat turn) forward qua `Bridge.OnEvent`:

```
POST http://127.0.0.1:5000/api/monitor/event
{
  "type": "buddy_event",
  "summary": "buddy turn assistant",
  "detail": { "evt": "turn", "role": "assistant", "content": "<rendered text>" }
}
```

Consumer phía Lamp có thể subscribe `buddy_event` cho TTS, transcript
memory, dashboard… — chưa wire cái nào.

### Vị trí trong LED priority

Hệ thống có 4-level LED hierarchy. Buddy ở level 1.5:

```
Level 0   Status LED   (error, OTA, boot, connectivity, listening)
Level 1   Agent emotion (OpenClaw [HW:/emotion:…])
Level 1.5 Buddy state  ← ở đây
Level 2   Local intent (voice "bật đèn xanh")
Level 3   Ambient breathing
```

Ambient service nghe monitor bus cho `buddy_state`:

| State buddy | Phản ứng ambient |
|---|---|
| `attention`, `busy` | Coi như `led_set` — pause ambient breathing. |
| `idle`, `sleep` | Coi như `led_off` — resume ambient. |
| `heart`, `celebrate` | Transient, tự unlock sau 3 s. |

Agent emotion vẫn thắng buddy; voice intent của user thắng cả 2.

---

## 8. Voice approval flow (UC-2)

```
1. Heartbeat đến với prompt != null
2. state → attention; bridge fire:
     HAL: blink cam + display "Approve <tool>?"
     Lamp:   POST /api/sensing/event { type:"buddy_approval", message:"Claude Desktop needs approval: …" }
3. OpenClaw route sensing event tới skill `claude-desktop-buddy`
4. Skill (SKILL.md tại claude-desktop-buddy/skill/):
     - Express emotion: curious 0.8
     - Đọc prompt qua TTS một cách tự nhiên
     - Đợi user nói "yes/approve/ok" hoặc "no/deny/skip"
5. Skill curl ngược lại:
     POST http://127.0.0.1:5002/claude-desktop/approve  {"id":"req_abc123"}   (hoặc /claude-desktop/deny)
6. buddy-plugin trả BLE PermissionDecision:
     {"cmd":"permission","id":"req_abc123","decision":"once" | "deny"}
7. Desktop unblock. State → heart (nếu user trả lời <5s) → busy → idle.
```

Tại sao route qua OpenClaw thay vì trả lời thẳng trong buddy:

- OpenClaw own TTS (strip markdown, voice character, queue logic).
- OpenClaw đã coordinate với state busy/listening của voice pipeline,
  nên câu hỏi approval không cắt ngang hội thoại đang chạy.
- OpenClaw có thể chọn *không* nói khi user vắng mặt (presence) — chỉ
  blink LED im lặng.
- Event vào flow-events log có sẵn để monitor.

### Vị trí skill

```
claude-desktop-buddy/skill/SKILL.md
```

Skill ship cùng buddy binary và **không** copy vào
`lamp/resources/openclaw-skills/`. OpenClaw đọc từ install dir của
buddy tại runtime (xem SKILL.md cho rule discovery chính xác).

---

## 9. HTTP API (port 5002)

| Method | Path | Mô tả | Body / Response |
|---|---|---|---|
| `GET` | `/health` | Liveness | `{status, ble_advertising, uptime_seconds}` |
| `GET` | `/status` | State buddy hiện tại | Xem bên dưới |
| `POST` | `/claude-desktop/approve` | Approve prompt đang pending | Body `{id}`. Trả `{ok}`. |
| `POST` | `/claude-desktop/deny` | Deny prompt đang pending | Body `{id}`. Trả `{ok}`. |
| `POST` | `/claude-code/notify` | Push hoạt động Claude Code (plugin) | Body `{title,subtitle,level,sound}`. Log; trả `{ok}`. HAL bridge chưa wire. |
| `POST` | `/claude-code/usage` | Push usage Claude Code (plugin) | Body `{five_hour,seven_day,reset_5h,reset_7d,sound}`. Log; trả `{ok}`. |

Response `/status`:

```json
{
  "state": "attention",
  "connected": true,
  "sessions_running": 1,
  "tokens_today": 8200,
  "pending_prompt": {
    "id": "req_abc123",
    "tool": "Edit",
    "hint": "server.go lines 10-20",
    "received_at": ""
  }
}
```

`/claude-desktop/approve` và `/claude-desktop/deny` reject `409 Conflict` nếu không có prompt pending
hoặc `id` không match prompt hiện tại — chặn skill OpenClaw act trên
prompt cũ.

---

## 10. Phối hợp phía Lamp

### Watcher đổi config (os/services/server/server.go)

Khi user setup device qua `POST /api/device/setup`, Lamp save
`device_id` vào `config/config.json` và notify config bus in-process.
Lamp server nghe bus đó, khi `device_id` transition thì chạy:

```
systemctl cat claude-desktop-buddy.service   # skip im lặng nếu chưa cài
systemctl restart claude-desktop-buddy
```

Buddy có dịp re-resolve `Claude-{deviceid}` về id mới được assign mà
không cần can thiệp thủ công. Lamp chưa cài buddy plugin thì pre-check
`systemctl cat` no-op.

### Endpoint system info Lamp

Buddy đọc `device_id` qua HTTP thay vì đọc file config trực tiếp:

```
GET http://127.0.0.1:5000/api/system/info
→ { "data": { "deviceId": "lamp-004", … } }
```

Cách này giữ buddy không biết schema config của Lamp.

---

## 11. Config

`/root/config/buddy.json` (tạo 1 lần bởi `setup-claude-desktop-buddy.sh`
hoặc `software-update claude-desktop-buddy`, tooling không bao giờ overwrite sau):

```json
{
  "enabled": true,
  "device_name": "Claude-{deviceid}",
  "http_port": 5002,
  "lelamp_url": "http://127.0.0.1:5001",
  "lamp_url": "http://127.0.0.1:5000",
  "approval_timeout_sec": 30,
  "led_mapping": {
    "sleep":     { "action": "off" },
    "idle":      { "action": "none" },
    "busy":      { "effect": "pulse",   "color": [0, 100, 255],   "speed": 0.8 },
    "attention": { "effect": "blink",   "color": [255, 80, 0],    "speed": 1.5 },
    "heart":     { "action": "solid",   "color": [255, 200, 100], "duration_ms": 3000 },
    "celebrate": { "effect": "rainbow", "speed": 2.0, "duration_ms": 3000 }
  }
}
```

`led_mapping` được parse và forward thẳng tới HAL; template mirror
đúng những gì `bridge.OnStateChange` đang làm trong code. `idle` là
`none` để ambient service giữ quyền điều khiển LED khi lamp không
actively phản ánh state Claude.

---

## 12. BLE library: tinygo vendored

Mình start với `tinygo.org/x/bluetooth v0.14.0` upstream. 2 feature cần
hoặc là TODO (`MinInterval` / `MaxInterval` advertising) hoặc thiếu hẳn
(flags BlueZ `secure-read` / `secure-write` characteristic). Mình
vendor lib vào `third_party/bluetooth/` và thêm vào. `go.mod` có:

```
replace tinygo.org/x/bluetooth => ./third_party/bluetooth
```

Patch:

- `gatts.go` — mở rộng `CharacteristicPermissions` từ 6 bit lên 8 với
  `CharacteristicSecureReadPermission` và
  `CharacteristicSecureWritePermission`.
- `gatts_linux.go` — thêm `"secure-read"` và `"secure-write"` vào
  array flag-string của BlueZ để 2 bit mới map lên representation
  D-Bus.

Advertising interval set qua route debugfs kernel
(`/sys/kernel/debug/bluetooth/hci*/adv_{min,max}_interval`) thay vì
patch tinygo — xem `tuneAdvIntervals()` trong `ble.go`. Cách này chạy
trên mọi BlueZ bất kể property D-Bus `MinInterval`/`MaxInterval` có
được honor hay không.

---

## 13. Chip / kernel notes (Orange Pi 4 Pro, AIC8820)

Orange Pi 4 Pro ship với chip BT AIC8820 (Aicsemi, manufacturer ID
2875), UART-attached. Quirks gặp phải:

- **Không có MAC factory**: `bdaddr` mặc định `10:11:12:13:14:15`.
  `btmgmt -i 0 public-addr <new>` trả `0x0c Not Supported`.
- **Không support LE Privacy**: `bluetoothd` log `Failed to set
  privacy: Rejected (0x0b)` lúc start. Cosmetic; LE advertising vẫn
  chạy.
- **Reset hazard**: restart `bluetoothd` / chip nhiều lần có thể đẩy
  controller vào state `UP RUNNING PSCAN` (chỉ classic) và từ chối LE
  advertising 1 lúc. Recovery: `sudo systemctl restart bluetooth &&
  sudo hciconfig hci0 reset && sudo systemctl restart claude-desktop-buddy`.

Raspberry Pi (Broadcom / RP1) không có quirks này và dùng MAC factory,
nhưng phần còn lại của integration giống hệt.

---

## 14. Triển khai

### systemd unit (`/etc/systemd/system/claude-desktop-buddy.service`)

```ini
[Unit]
Description=Lamp Claude Desktop Buddy
After=bluetooth.target os-server.service
Wants=bluetooth.target

[Service]
ExecStart=/opt/claude-desktop-buddy/buddy-plugin -config /root/config/buddy.json -log /var/log/claude-desktop-buddy.log
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

### Đường dẫn install

| Path | Mục đích |
|---|---|
| `/opt/claude-desktop-buddy/buddy-plugin` | Binary (`linux/arm64`) |
| `/opt/claude-desktop-buddy/VERSION_BUDDY` | Version stamp match OTA metadata |
| `/opt/claude-desktop-buddy/chars/<name>/` | Character pack nhận về từ folder push |
| `/root/config/buddy.json` | Runtime config (giữ qua OTA) |
| `/var/lib/claude-desktop-buddy/stats.json` | Counter approve/deny lifetime (giữ qua OTA + reset config) |
| `/var/log/claude-desktop-buddy.log` | Log rotate (2 MB × 10 backup) |

### Lệnh update

- Cài lần đầu: `setup-claude-desktop-buddy.sh` (download từ OTA
  metadata, tạo service, không động config nếu đã tồn tại).
- Update sau: `software-update claude-desktop-buddy` (chỉ binary + version stamp
  + restart service; config không bao giờ bị overwrite).

---

## 15. Rủi ro & gap đã biết

| Rủi ro / gap | Ảnh hưởng | Trạng thái |
|---|---|---|
| Connection unencrypted (`sec: false`) | Panel hiện cảnh báo "Connection is unencrypted"; data GATT đi không mã hóa | Chấp nhận cho đến khi Desktop side drive SMP |
| Mac BT cache theo BD address | Đổi tên trên Pi không refresh tên Mac thấy | Forget device trên Mac sau khi rename |
| AIC8820 MAC giả | Nhiều lamp AIC8820 trên cùng Mac sẽ đụng cache | Out of scope tới khi vendor support đổi MAC |
| BLE write-without-response packet loss | Heartbeat / event dài bị mid-corruption | `ParseOrSalvage` cứu nếu được; drop kèm category log nếu không |
| LED conflict với agent emotion | Buddy + emotion cùng touch LED | Buddy ở dưới emotion qua priority monitor bus |
| Voice approval đụng TTS đang chạy | Câu hỏi approval cắt ngang hội thoại | Skill OpenClaw route qua TTS queue, respect busy state |

---

## 16. Tiêu chí thành công — trạng thái hiện tại

- [x] Claude Desktop thấy `Claude-XXX` trong Hardware Buddy picker
- [x] Pair thành công, tự reconnect sau khi Mac wake / Pi reboot
- [x] LED phản ánh state mà không đè agent emotion hoặc ambient
- [x] Voice approve / deny route qua OpenClaw, end-to-end
- [x] Token count + sessions chạy track + expose qua `/status` (chưa có display trên lamp)
- [x] Buddy crash không ảnh hưởng Lamp server chính
- [x] OpenClaw giảm proactive behavior khi Desktop busy
- [x] Chat turn (user / assistant / tool blocks) stream vào Lamp monitor bus
- [x] Folder push character pack lưu vào `chars/<name>/`
- [x] UC-9 TTS narration trạng thái (vi/en/zh) qua cache HAL + emotion khi done
- [x] Counter approve/deny giữ được qua restart (`/var/lib/claude-desktop-buddy/stats.json`)
- [ ] UC-8 đọc reply assistant qua TTS — kế tiếp
- [ ] GATT link bonded encrypted (`sec: true`) — defer
- [ ] Presence feedback Lamp → Desktop — mở rộng protocol tương lai
- [ ] Inject transcript context vào OpenClaw — tương lai
