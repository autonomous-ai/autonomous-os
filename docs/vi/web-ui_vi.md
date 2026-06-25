# Web UI — Monitor Dashboard

## Ngày cập nhật: 2026-06-24

---

## 1. Tổng Quan

Web UI của thiết bị là một React SPA (Single Page Application) được build bằng **React 19 + TypeScript + Vite + Tailwind CSS 4**, phục vụ hai mục đích:

1. **Setup flow** — Onboarding WiFi, LLM provider, messaging channel (các trang `/setup/*`)
2. **Monitor Dashboard** — Theo dõi trạng thái thiết bị real-time (`/monitor`)

File build output (`dist/`) được nginx serve tại root `/` trên thiết bị.

### 1.1 Tiêu đề tab trình duyệt

Tiêu đề tab trình duyệt (`document.title`) hiển thị đúng theo page/tab đang focus, để mở nhiều tab thiết bị vẫn phân biệt được. Dùng hook chung `useDocumentTitle` (`os/services/web/src/hooks/useDocumentTitle.ts`); format: `Lamp · <segment>[· <sub-segment>]`.

| Route / trạng thái | Title |
|--------------------|-------|
| `/setup` (và `/` khi chưa provision) | `Lamp · Setup` |
| `/monitor#<section>` (theo section đang chọn) | `Lamp · <tên section>` — ví dụ `Lamp · Chat`, `Lamp · Overview`, `Lamp · Info`, `Lamp · Flow`, `Lamp · Users`, `Lamp · Camera`, `Lamp · Sensing`, `Lamp · Analytics`, `Lamp · Servo`, `Lamp · Logs`, `Lamp · CLI` |
| `/setting#<section>` (Settings, theo section đang chọn) | `Lamp · Settings · <tên section>` — ví dụ `Lamp · Settings · General`, `Lamp · Settings · Wi-Fi`, `Lamp · Settings · AI Brain`, `Lamp · Settings · Language`, `Lamp · Settings · Voice`, `Lamp · Settings · My Voice`, `Lamp · Settings · Face`, `Lamp · Settings · Channels`, `Lamp · Settings · MQTT` |
| `/gw-config` | `Lamp · GW Config` |

`<title>Lamp Setup</title>` tĩnh trong `index.html` chỉ là fallback trước khi React mount; hook sẽ ghi đè khi mount và khôi phục title cũ khi unmount.

---

## 2. Cấu Trúc Thư Mục

```
os/services/web/
├── src/
│   ├── pages/
│   │   ├── Monitor.tsx        # Dashboard monitor (file chính)
│   │   └── ...                # Các trang setup
│   ├── components/
│   │   └── ui/                # shadcn/ui components
│   ├── lib/
│   │   └── i18n.ts            # Bản địa hóa chuỗi UI (en/vi/zh-CN/zh-TW, fallback tiếng Anh)
│   ├── index.css              # Global styles + theme variables
│   └── main.tsx
├── vite.config.ts
└── package.json
```

---

## 3. Monitor Dashboard (`/monitor`)

### 3.1 Thiết Kế Tổng Thể

Monitor dùng dark theme riêng với class `.lm-root` (định nghĩa trong `index.css`), **không dùng Tailwind** — toàn bộ styling dùng inline styles với CSS variables `--lm-*`.

Layout: **Sidebar 192px cố định + Main area co giãn**, chiều cao 100vh.

### 3.2 Sidebar Navigation

4 section có thể chuyển đổi bằng local state (`section: Section`):

| Icon | Section | Nội dung |
|------|---------|---------|
| ◈ | Overview | Tổng quan toàn bộ hệ thống |
| ⬡ | System | CPU/RAM/Temp chi tiết + lịch sử |
| ◎ | Workflow | OpenClaw event feed real-time |
| ⬟ | Camera | MJPEG stream + Display LCD |

Góc dưới sidebar hiển thị trạng thái OpenClaw (online/offline) và thời điểm cập nhật gần nhất.

### 3.3 Dark Theme Variables

Định nghĩa tại `.lm-root` trong `index.css`:

```css
--lm-bg:          #0C0B09   /* Background chính */
--lm-sidebar:     #111009   /* Sidebar */
--lm-card:        #17160F   /* Card background */
--lm-surface:     #1E1D14   /* Surface bên trong card */
--lm-border:      #2A2820   /* Border */
--lm-border-hi:   #3A3828   /* Border highlight */
--lm-amber:       #F59E0B   /* Màu chủ đạo (warm lamp) */
--lm-amber-dim:   rgba(245,158,11,0.12)
--lm-amber-glow:  rgba(245,158,11,0.35)
--lm-teal:        #2DD4BF
--lm-green:       #34D399
--lm-red:         #F87171
--lm-blue:        #60A5FA
--lm-purple:      #A78BFA
--lm-text:        #F0EEE8
--lm-text-dim:    #9A9080
--lm-text-muted:  #504A3C
```

### 3.4 Settings (`/setting`) — shell dùng chung

Settings **không phải là một trang riêng**. Nó là một khu vực (area) của chính shell Monitor (`os/services/web/src/pages/monitor/index.tsx`), truy cập tại route `/setting`. Trong `App.tsx`, `/monitor` và `/setting` là các route con của một layout route duy nhất có element render `<Monitor/>`; React Router giữ element đó luôn mounted khi chỉ đường dẫn con thay đổi, nên sidebar **không** bị remount khi chuyển giữa Monitor và Settings (không có hiện tượng nháy toàn trang). Shell suy ra khu vực — `"monitor"` hoặc `"setting"` — từ `useLocation().pathname`.

Nhóm Settings có thể thu gọn nằm trong `NAV` của sidebar dùng chung (`os/services/web/src/pages/monitor/types.ts`). Bấm một mục Settings sẽ điều hướng tới `/setting` và render `SettingsPanel` (`os/services/web/src/pages/settings/SettingsPanel.tsx`) ở khu vực chính; bấm một mục Monitor sẽ điều hướng tới `/monitor`.

**Sơ đồ URL hash** — section trong bộ nhớ vẫn giữ id nội bộ `settings:*`, nhưng URL hash dùng nhãn ngắn trong khu vực setting (các helper `sectionToHash`/`hashToSection` trong `types.ts`):

| Mục | URL |
|-----|-----|
| General | `/setting#general` (nội bộ `settings:device`) |
| Wi-Fi | `/setting#wifi` |
| My Voice | `/setting#voice` |
| Face | `/setting#face` |
| AI Brain | `/setting#llm` |
| Runtime | `/setting#runtime` |
| Language | `/setting#stt` |
| Voice | `/setting#tts` |
| Realtime | `/setting#realtime` |
| Channels | `/setting#channel` |
| MQTT | `/setting#mqtt` |

Các mục Monitor được serialize thành id thuần, ví dụ `/monitor#overview`, `/monitor#system`, `/monitor#flow`. Mặc định: `/monitor` không có hash / hash không hợp lệ → `overview`; `/setting` không có hash / hash không hợp lệ → `general` (URL được chuẩn hóa thành `/setting#general`). Deep-link (ví dụ `/setting#wifi`) và nút back/forward của trình duyệt được tôn trọng qua một effect dựa trên `useLocation`. Người dùng không-debug chỉ thấy các mục trong `PUBLIC_SECTIONS` (gồm Chat, Overview, Info, Flow, Camera, Users, Bluetooth, **Logs**, **CLI**, và các mục Settings công khai General/Wi-Fi/My Voice/Face); `?debug=true` mở khóa phần còn lại (Sensing, Analytics, Servo, API Docs, Agent gateway, và các mục Settings sâu hơn AI Brain/Runtime/Language/Voice/Realtime/Channels/MQTT).

Trang `/edit` độc lập (cũ) đã bị gỡ bỏ; `SettingsPanel` của nó giờ chỉ truy cập được qua các tab `/setting` bên trong Monitor. `/edit` (và link "update →" trong Setup) giờ redirect tới `/setting`.

---

## 4. Polling & Data Sources

Monitor poll API system/HW mỗi **3 giây**. Flow dùng hybrid theo file: REST seed + stream live.

### 4.1 OS Server (Go, port 5000, prefix `/api`)

| Endpoint | Dữ liệu |
|----------|---------|
| `GET /api/system/info` | CPU load, RAM (KB), nhiệt độ, uptime, goroutines, version, deviceId, capabilities (tên các capability đã khai báo — cả Monitor lẫn trang Edit/Settings đều ẩn/hiện tab phần cứng theo danh sách này; xem hook dùng chung `useCapabilities`) |
| `GET /api/system/network` | SSID, IP, public IP, Tailscale IP, signal (dBm), internet (bool) |
| `GET /api/openclaw/status` | name, connected (bool), sessionKey (bool), version, emotion, uptime (uptime kết nối WS phía OS server, giây), agentUptime (uptime tiến trình OpenClaw lấy từ `server.uptimeMs` trong hello-ok, giây — không reset khi OS server restart) |
| `GET /api/openclaw/recent` | Các flow event mới nhất từ JSONL của ngày hiện tại (`local/flow_events_<date>.jsonl`) |
| `GET /api/openclaw/flow-events?date=YYYY-MM-DD&last=500` | API flow theo file dùng cho seed/history của Flow |
| `GET /api/openclaw/flow-stream` | Stream live theo file (SSE) khi JSONL thay đổi |
| `GET /api/openclaw/events` | SSE từ monitor bus, giữ để tương thích |
| `POST /api/system/force-update` | Kích hoạt kiểm tra OTA qua bootstrap worker (proxy tới `localhost:8080/force-check`) |

> **Lưu ý format**: OS server API trả `{ status: 1, data: <payload>, message: null }` khi thành công.

### 4.2 HAL (Python/FastAPI, port 5001, prefix `/hw`)

| Endpoint | Dữ liệu |
|----------|---------|
| `GET /hw/health` | Trạng thái 8 hardware: servo, led, camera, audio, sensing, voice, tts, display |
| `GET /hw/presence` | state, enabled, seconds_since_motion |
| `GET /hw/voice/status` | voice_available, voice_listening, tts_available, tts_speaking |
| `GET /hw/servo` | available_recordings, current, bus_connected, robot_connected |
| `POST /hw/servo/upload` | Upload recording CSV (`timestamp` + cột `<joint>.pos`) để thêm/replace animation |
| `GET /hw/display` | mode, hardware, available_expressions |
| `GET /hw/audio/volume` | control, volume (0–100) |
| `GET /hw/led/color` | led_count, color [R,G,B], hex (#rrggbb) |

---

## 5. Các Section Chi Tiết

### 5.1 Overview Section

Gồm các card:

**OpenClaw AI**
- Trạng thái connected/disconnected
- Tên agent
- Session key: Acquired / Pending

**Network**
- SSID + Signal bars (4 mức dựa trên dBm)
- IP address
- Tailscale IP (chỉ hiện khi `tailscale ip -4` trả về địa chỉ — hoạt động
  cả ở kernel mode lẫn userspace-networking mode)
- Internet status

> Setup gate (`App.tsx`) tự redirect từ AP/host khác sang LAN IP của thiết bị,
> nhưng bỏ qua redirect khi hostname nằm trong dải Tailscale CGNAT
> `100.64.0.0/10` — truy cập qua Tailscale được coi là remote access có chủ ý.

**Presence**
- State (active/idle)
- Sensing enabled/disabled
- Thời gian kể từ lần detect chuyển động cuối

**Voice & TTS**
- Mic available + đang listening (badge LIVE)
- TTS available + đang speaking (badge SPEAKING)
- Volume hiện tại

**Hardware** (card ngang)
- 8 badge: Servo / LED / Camera / Audio / Sensing / Voice / TTS / Display
- **LED color swatch**: ô màu vuông bo góc hiển thị màu hiện tại của dải LED, kèm hex code. Lấy từ `GET /hw/led/color`.

**Scene** (preset ánh sáng)
- Hiển thị danh sách scene preset (reading, focus, relax, movie, night, energize). Lấy từ `GET /hw/scene`.
- Bấm nút để kích hoạt scene qua `POST /hw/scene` với `{"scene": "<tên>"}`.
- Scene đang active được highlight màu amber.

**Servo Pose**
- Pose đang chạy (current)
- Danh sách servo recordings/animations (từ `GET /hw/servo`)
- Mỗi recording có thể phát qua `POST /hw/servo/play` (tên recording)
- UI có nút `Upload CSV` để thêm/replace recording qua `POST /hw/servo/upload` (multipart: `file`, `recording_name`)

**Display Eyes**
- Expression đang hiển thị (mode)
- Danh sách expressions available

> **Card gate theo capability.** Các card phần cứng ở Overview bị ẩn trên thiết bị
> không có capability tương ứng, nên trang chỉ hiển thị thứ thiết bị thực sự làm được
> (ví dụ intern-v2 không có servo, scene, hay expression):
> - **Emotion** và **Servo Pose** gate theo capability đã khai báo (`expression` / `motion`) lấy từ `GET /api/system/info` → `capabilities`.
> - **Scene** là route *bên trong* capability `light` (lamp khai báo `light:[led,scene]`; intern-v2 khai báo `light:[led]`), nên không phân biệt được qua danh sách capability — card chỉ render khi `GET /hw/scene` trả về danh sách scene.

**System quick stats**
- CPU, RAM, Temp, Uptime dạng pill

### Sidebar Footer

Dưới nav items và trạng thái OpenClaw, sidebar hiển thị version của cả 3 repo:
- **Web** (teal): inject lúc build từ `package.json` qua Vite `define` (`__WEB_VERSION__`)
- **OS server** (amber): từ `GET /api/system/info` → field `version` (Go ldflags)
- **HAL** (blue): từ `GET /api/system/info` → field `halVersion`. OS server tự gọi `:5001/version` của HAL qua loopback mỗi phút 1 lần (cache) rồi re-expose qua API của OS server, browser không cần truy cập trực tiếp `/hw/*` (nginx chặn `/hw/` chỉ cho loopback).
- **Force Update** button: gọi `POST /api/system/force-update` → bootstrap kiểm tra OTA. Hiện "Checking…" khi đang xử lý, sau đó "Triggered"/"Failed" trong 3 giây.

### 5.2 System Section

**Performance** — 3 GaugeRing SVG:
- CPU: màu amber, hiện `%`
- Memory: màu blue, detail `used/total MB` (chuyển đổi từ KB: `value / 1024`)
- Temp: màu teal (< 70°C) hoặc red (≥ 70°C), scale 0–85°C

**CPU History / RAM History** — Sparkline chart (area + line):
- Lưu 60 điểm lịch sử (`HISTORY_LEN = 60`)
- Cập nhật mỗi 3 giây

**Process**: goroutines, uptime, version, deviceId
**Network Detail**: SSID, IP, signal, internet

### 5.3 Workflow Section

Flow feed hybrid theo file:

| Type | Màu | Ý nghĩa |
|------|-----|---------|
| `lifecycle` | amber | Agent bắt đầu / kết thúc run |
| `tool_call` | teal | AI gọi một tool |
| `thinking` | purple | AI đang suy nghĩ (streaming) |
| `assistant_delta` | blue | AI đang trả lời (streaming delta) |
| `chat_response` | green | Chat response final |

Mỗi event hiển thị: type badge, phase (nếu có), runId (8 ký tự đầu), timestamp, summary text, error (nếu có).

- Load ban đầu/history qua `GET /api/openclaw/flow-events`.
- Update live qua `GET /api/openclaw/flow-stream` (SSE bắn khi file đổi).
- Chỉ fallback poll 2 giây khi stream bị ngắt.
- Turn/event hiển thị được suy ra hoàn toàn từ JSONL flow log.

**Turn Pipeline (SVG)** — `FlowDiagram` trong `os/services/web/src/pages/Monitor.tsx`. Bố cục đầy đủ (ba vùng OS server / HAL / OpenClaw, lưới cột OpenClaw, Cron thuộc OS server, hàng HAL thẳng Tool, bảng tọa độ) nằm trong **`docs/flow-monitor.md`**; tóm tắt tiếng Việt: **`docs/vi/flow-monitor_vi.md`**.

Hành vi gom nhóm Turn Pipeline:
- Turn vẫn bắt đầu từ các event input/trigger (`sensing_input`, `chat_input`, `schedule_trigger`, ...).
- UI giờ neo mỗi turn theo `run_id` đầu tiên phát hiện được (ở root event hoặc trong `detail`).
- Với user mic actions: mỗi `sensing_input` dạng `[voice]` / `[voice_command]` (và `voice_pipeline_start`) tạo một turn riêng, ngay cả khi các event có thể đang chung `run_id`.
- Với web monitor chat: mỗi `sensing_input` dạng `[web_chat]` tạo boundary turn riêng (icon 🖥, filter category **Web**) nên không bị merge chung với turn voice/sensing kề nhau.
- Với user chat actions: mỗi `chat_input` (telegram input) tạo một boundary turn riêng, nên sẽ không bị merge chung với turn voice kề nhau dù OpenClaw có reuse `run_id`.
- Nếu event phía sau có `run_id` khác, Monitor sẽ tách thành một turn agent suy diễn mới.
- **Badge loại turn** (`motion`, `voice`, …): cùng một `run_id` có thể vừa motion (camera) vừa voice; trước đây segment đầu quyết định badge nên dễ hiện `motion` dù user vừa nói. Sau khi gom turn, nếu có bất kỳ `sensing_input` kiểu `[voice]` / `[voice_command]` thì badge ưu tiên voice hơn motion.
- `OUT` chỉ lấy từ `tts_send`/`intent_match` cùng `run_id` với turn (hoặc event không có run_id), tránh ghép nhầm IN/OUT giữa các turn.
- Token LLM hiển thị trên các node LLM (Agent Call / Thinking / Response): `in/out` và nếu có `token_usage` thì thêm `cache read/write` + `total`.
- Với Telegram input, summary placeholder kiểu `[telegram]` sẽ không còn khóa cứng trường `IN`; nếu event đến sau cùng `run_id` có message thật, UI sẽ thay placeholder bằng nội dung đó (và sẽ override cả sensing_input text như SOUND nếu cùng nằm trong một UI turn). Nếu message Telegram bị thiếu hoàn toàn (ghost turn) thì turn type sẽ thành `unknown` để tránh hiểu nhầm “TG IN”.
- Fallback tạm thời: khi không lấy được text Telegram, UI sẽ hiển thị `Message content from telegram`.
- Turn badge luôn render dòng `IN`; nếu thiếu input, UI sẽ hiển thị `Input not captured`.
- Header Flow Panel: `↓ Bundle`, `full day`, `🗑 Log`.
- `↓ Bundle` = **một lần bấm tải hai file**: JSONL server (fetch + blob, `flow-logs?last=500`) và JSON snapshot trong browser (`events` + `groupIntoTurns` → `lamp_flow_ui_snapshot_*.json`).
- `full day` = cả file JSONL trong ngày.
- Nút `🗑 Log` sẽ hỏi xác nhận trước, gọi `DELETE /api/openclaw/flow-logs` để truncate flow log, rồi xóa events đang hiển thị trong Flow UI.
- Danh sách Turn history: hiển thị **tất cả turn** trong ngày (mới nhất ở trên), suy ra từ **10 000 event** cuối — đủ cho cả ngày hoạt động bình thường.
- Bộ nhớ event của Flow được giới hạn 10 000 events.
- Heuristic ghép turn Telegram: nếu turn Telegram fallback (không có text input thật) đứng ngay trước turn có output agent trong vòng 30 giây, Monitor sẽ ghép thành 1 turn để câu trả lời đi cùng input Telegram.

### 5.4 Camera Section

- **Camera Stream**: MJPEG live stream từ `GET /hw/camera/stream` (downscaled + throttled; mặc định ~10fps, ~320px chiều ngang)
- **Display Eyes (GC9A01)**: Snapshot màn hình tròn 1.28" từ `GET /hw/display/snapshot`, hiển thị dạng hình tròn với amber glow. Có nút Refresh.
- **Camera Snapshot**: Ảnh tĩnh từ `GET /hw/camera/snapshot`, có nút Capture để chụp mới.

### 5.5 Logs Section

- Tab log runtime cho HAL, OS (os-server), và OpenClaw service logs.
- Mỗi panel stream qua SSE (`GET /api/logs/stream?source=<source>`) với fallback polling.
- Hỗ trợ filter theo level (ALL/DEBUG/INFO/WARN/ERROR) và tìm kiếm text/regex.

> **Lưu ý**: Camera có vai trò kép — (1) hiển thị live stream cho user xem, (2) nguồn dữ liệu sensing tự động. Sensing service đọc frame từ camera mỗi 2s để detect motion, face (Haar cascade), và light level. Khi phát hiện sự kiện đáng kể (người xuất hiện, chuyển động lớn), auto-snapshot full-resolution JPEG được gửi kèm event tới OpenClaw AI để phân tích bằng vision.

### 5.6 Chat Section

Giao diện chat tương tác với agent. Layout: sidebar (danh sách hội thoại) + vùng chat chính.

**Hội thoại**
- Nhiều hội thoại lưu trong localStorage (tối đa 50, mỗi cái 200 tin nhắn)
- Sidebar: tìm kiếm, ghim, đổi tên (double-click), xóa (xác nhận 2 lần), xuất TXT
- Nhóm theo ngày: Today / Yesterday / This week / Older, ghim lên đầu. Mỗi header nhóm có đường kẻ mảnh và số lượng item.
- Mỗi dòng hiển thị một chấm avatar màu (hash từ id hội thoại, theo palette), tiêu đề, nhãn thời gian tương đối đã bản địa hóa (`vừa xong` / `5 phút` / `2 giờ` / `hôm qua` / `3 ngày`, ẩn khi hover), và preview tin nhắn cuối. Hội thoại đang mở được đánh dấu bằng thanh dọc amber bên trái.
- Phím tắt: Cmd/Ctrl+N tạo chat mới
- Sidebar thu gọn được

**Nhập tin nhắn**
- Textarea, Shift+Enter xuống dòng, Enter gửi
- Đính kèm file/ảnh (tối đa 10 MB): nút, kéo thả, dán từ clipboard
- Gửi qua `POST /api/sensing/event` với `type: "web_chat"`. Handler mark run qua `MarkWebChatRun(runID)` để reply của agent bị suppress TTS (chỉ hiện trong UI này) và bỏ qua wake greeting / opening filler. Web chat có image attach: lưu vào `/tmp/web-chat-*.jpg` và gắn vào tin nhắn agent qua `[image: <path>]`.

**Streaming real-time**
- **Thinking indicator**: khối tím thu gọn được, hiển thị reasoning tokens của LLM khi stream (`thinking` events). Click mở rộng toàn bộ (max-height 200px, scroll). Tự ẩn khi response hoàn tất.
- **Assistant delta streaming**: text response hiện từng token qua `assistant_delta` events, thay vì đợi response cuối cùng. Fallback sang `chat_response` partial cho đường non-agent.
- **Tool call chips**: badge màu teal hiển thị các tool agent gọi trong response (emotion, LED, servo, audio, v.v.). Hiển thị phía trên bubble tin nhắn khi đang stream, lưu lại trên tin nhắn đã hoàn tất. Một tool hiển thị thành một chip; **từ hai tool trở lên sẽ gom thành một pill tóm tắt** ("N steps" với các icon tool xếp chồng + marker đang chạy/`DONE`), bấm vào để mở ra các chip riêng lẻ.

**Xử lý response**
- Theo dõi response qua `runId` correlation trên SSE events
- HW control markers inline (`[HW:/emotion:...]`) được lọc bỏ khỏi text hiển thị
- Timeout 30 giây: nếu đã nhận streaming text thì hiển thị phần đó; nếu không thì báo lỗi với nút retry
- Local intent fast path: response dưới 50ms bypass agent
- Busy/dropped: hiển thị "busy — try again"
- Markdown: bold, italic, inline code (tô màu amber), code block (monospace), URL, danh sách, và bảng (header có nền + hàng zebra)

**Empty State & Gợi ý**
- Khi cuộc trò chuyện chưa có tin nhắn, khu vực chat hiển thị một quả cầu assistant lớn đang "thở", tiêu đề/phụ đề đã bản địa hóa, và bốn **chip gợi ý** bấm được. Bấm một chip sẽ điền sẵn vào ô nhập (không tự gửi) để người dùng chỉnh trước.

**Bản địa hóa (i18n)**
- Các chuỗi UI riêng của chat (tiêu đề/phụ đề empty-state, chip gợi ý, trạng thái "đang suy nghĩ"/"trực tuyến" trên thanh trên cùng) được bản địa hóa qua `src/lib/i18n.ts` — một module nhẹ tự viết, mô phỏng convention của backend Go `os/services/lib/i18n` (mã chuẩn `en` / `vi` / `zh-CN` / `zh-TW`, chuẩn hóa alias, **fallback về tiếng Anh** theo từng key).
- Ngôn ngữ active được lấy từ trường `stt_language` trong device config (cùng nguồn mà `i18n.Lang()` của Go đọc từ `config.STTLanguage`) qua `setLanguage()` trong `App.tsx` khi load config lần đầu, và Chat section áp lại từ lần fetch config của chính nó. Các component đọc chuỗi qua hook `useT()`, hook này re-render khi ngôn ngữ được xác định.
- Module i18n này hiện chỉ phủ các chuỗi chat thêm vào trong đợt redesign; phần còn lại của UI Monitor vẫn hardcode tiếng Anh.

**Luồng dữ liệu**
```
Chat UI → POST /api/sensing/event → SensingHandler
  → openclaw.SendChatMessage() → WebSocket chat.send → OpenClaw
  → Response stream qua WebSocket (thinking → assistant deltas → lifecycle end)
  → SSE /api/openclaw/flow-stream → Chat UI cập nhật tin nhắn real-time
```

---

## 6. LED Color API

### Vấn đề
`GET /hw/led` gốc chỉ trả `{ led_count: 64 }` — không có thông tin màu hiện tại.

### Giải pháp
Thêm `GET /hw/led/color` vào `os/hal/server.py`:

```python
@app.get("/led/color", response_model=LEDColorResponse, tags=["LED"])
def get_led_color():
    """Get the current LED color (last color set on the strip)."""
```

**Ưu tiên lấy màu:**
1. `sensing_service.presence._last_color` — màu base được track khi AI set
2. Fallback: `rgb_service.strip.getPixelColor(0)` — đọc trực tiếp từ hardware

**Tracking đã được bổ sung cho:**
- `POST /led/solid` ✅ (đã có từ trước)
- `POST /scene` ✅ (đã có từ trước)
- `POST /emotion` ✅ (bổ sung thêm — đây là path AI dùng nhiều nhất)

> **Lưu ý**: `GET /hw/led/color` là **read-only**, monitor chỉ đọc, không set màu.

---

## 7. Reusable Components (nội bộ Monitor.tsx)

| Component | Mô tả |
|-----------|-------|
| `GaugeRing` | SVG ring chart với drop-shadow glow, transition 0.7s |
| `Sparkline` | SVG area + line chart, nhận mảng số |
| `HWBadge` | Badge xanh/đỏ cho hardware status |
| `StatusDot` | Chấm tròn xanh/đỏ với glow |
| `SignalBars` | 4 bar WiFi signal (ngưỡng: -50/-65/-75/-85 dBm) |
| `StatPill` | Row label + value trong card |

---

## 8. Global Source Footer (Tuân thủ GPL v3 §6)

`os/services/web/src/components/SourceFooter.tsx` là một link nhỏ `position: fixed`, mount tại App root (`App.tsx`, ngoài `<Routes>`), nên xuất hiện ở mọi trang — Setup, Login, Monitor, GwConfig.

Render tại `bottom: 6px, right: 8px` với chữ monospace 10px và opacity `0.7` — ai cần là thấy nhưng không đè form action buttons (Back / Next / Setup / Save) hoặc scroll. Link target: `https://github.com/autonomous-ai/autonomous-os`.

Lý do tồn tại: HAL Python (`os/hal/`) ship dưới GPL v3, bake sẵn vào image board. GPL §6 yêu cầu người nhận binary phải biết source code tương ứng ở đâu. Footer thỏa mãn lựa chọn "written offer" bằng cách expose URL repo public ngay trên thiết bị. Xem thêm `scripts/release/tag-release.sh` + `Makefile:tag-release` cho phần map version → commit.

---

## 9. Build & Deploy

```bash
# Build production
make web-build        # tsc + vite build → os/services/web/dist/

# Deploy lên Pi
make web-deploy       # web-build + rsync dist/ → /usr/share/nginx/html/setup/

# Deploy HAL (khi thay đổi server.py)
make hal-deploy       # rsync + pip install + systemctl restart hal.service
```

> Deploy dùng `PI_HOST=lamp.local` (mDNS). Nếu không resolve được, dùng IP trực tiếp:
> `PI_USER=root PI_HOST=<DEVICE_IP> make web-deploy`
