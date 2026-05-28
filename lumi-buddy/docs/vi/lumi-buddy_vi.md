# Lamp Buddy — Remote Computer Use cho Lamp

> **Trạng thái:** Đang thiết kế — đang scope MVP
> **Cập nhật:** 2026-05-21
> **Owner:** Leo
> **Liên quan:** [Kế hoạch MVP Lamp Buddy](./lumi-buddy-mvp_vi.md)

Tài liệu này lưu lại toàn bộ thảo luận thiết kế tính năng **Lamp Buddy**: một app native chạy thường trú trên máy tính người dùng, cho phép Lamp điều khiển desktop (mở app, vào web, gõ text, …) — kiểu TeamViewer nhưng được điều khiển bằng giọng nói/AI thông qua đèn.

Kế hoạch implement MVP nằm ở [`lumi-buddy-mvp_vi.md`](./lumi-buddy-mvp_vi.md). Doc này là tham chiếu dài về *lý do tại sao* kiến trúc lại như vậy.

---

## 1. Mục tiêu & không phải mục tiêu

### Mục tiêu
- Lamp điều khiển được máy tính qua voice ("mở Chrome", "vào Gmail", "join Google Meet", "gõ X", "đóng Slack")
- Hoạt động với mọi app macOS (không chỉ browser)
- LAN-only, dựa trên pairing — không qua relay server, không qua cloud
- Mac-first cho MVP; Windows/Linux để v1.2+

### Không phải mục tiêu (MVP)
- Điều khiển dựa trên hình ảnh (Claude Computer Use API loop screenshot → click)
- Stream màn hình real-time về lamp
- Hỗ trợ Windows / Linux
- Nhiều buddy trên 1 lamp (gia đình nhiều máy)
- Binary có chữ ký Apple / notarize — right-click → Open là OK với MVP self-host
- AppleScript executor vượt khuôn khổ vài case có sẵn
- Chạy shell command tùy ý (quá nguy hiểm, để sau với flag rõ ràng)

---

## 2. Use case (MVP)

| Voice (ví dụ) | Chuỗi action |
|---------------|--------------|
| "Mở Chrome và vào Gmail" | `open_app(Google Chrome)` → `open_url(https://gmail.com)` |
| "Tự động join Google Meet" | `open_url(https://meet.google.com/<last-or-configured>)` |
| "Mở Spotify" | `open_app(Spotify)` |
| "Gõ 'hello world' vào ô đang focus" | `type_text("hello world")` |
| "Đóng Slack" | `close_app(Slack)` |
| "Bật Do Not Disturb" | `applescript(...)` (whitelisted) |
| "Hiện noti 'meeting in 5 min'" | `notification(title, body)` |

Use case MVP KHÔNG hỗ trợ (chờ phase vision):
- "Đọc email mới nhất tóm tắt cho tôi" (cần đọc màn hình)
- "Search Google nội dung … rồi click kết quả đầu" (cần click theo vision)
- "Highlight đoạn này rồi copy" (cần biết trạng thái selection)

---

## 3. Kiến trúc

```
┌─────────────────────────┐         ┌────────────────────────────┐
│  User                   │         │  Mac (máy người dùng)      │
│    │ voice              │         │  ┌──────────────────────┐  │
│    ▼                    │         │  │ lumi-buddy.app       │  │
│  ┌─────────────────┐    │         │  │ (Swift, menu bar)    │  │
│  │ Lamp (Pi)       │    │         │  │                      │  │
│  │                 │    │         │  │  ┌────────────────┐  │  │
│  │  lelamp (Py)    │    │         │  │  │ Pairing & WS   │  │  │
│  │    └─ STT/TTS   │    │ ◀──WS───┼──┼──┤ client         │  │  │
│  │                 │    │         │  │  └────────────────┘  │  │
│  │  lumi (Go)      │    │         │  │  ┌────────────────┐  │  │
│  │    ├─ OpenClaw  │    │         │  │  │ Executors      │  │  │
│  │    │   └─ skill │    │         │  │  │  NSWorkspace   │  │  │
│  │    │   "compu-  │    │         │  │  │  CGEvent       │  │  │
│  │    │    ter-use"│    │         │  │  │  AppleScript   │  │  │
│  │    └─ buddy svc │    │         │  │  └────────────────┘  │  │
│  └─────────────────┘    │         │  └──────────────────────┘  │
│         ▲               │         │            │               │
│         │ mDNS          │         │            ▼               │
│         │ lamp-xxxx     │         │   App macOS (Chrome,       │
│         │ .local        │         │   Finder, Spotify, …)      │
└─────────────────────────┘         └────────────────────────────┘
```

### Luồng happy-path

1. User nói: "Mở Chrome trên máy tính và vào Gmail"
2. Mic ở lelamp → STT
3. Transcript → OpenClaw → match skill `computer-use`
4. Skill parse thành command, gọi Lamp Go:
   `POST /api/buddy/command {action:"open_app", params:{app:"Google Chrome"}}`
   `POST /api/buddy/command {action:"open_url", params:{url:"https://gmail.com"}}`
5. Buddy dispatcher của Lamp Go tra cứu buddy đã pair trong registry WS, gửi command qua WS đang mở
6. lumi-buddy decode JSON, dispatch cho executor (NSWorkspace, CGEvent, …), thực thi
7. Buddy trả về `{ok:true}` qua cùng WS
8. Skill nhận kết quả, trả về câu confirm TTS-friendly
9. lelamp đọc: "Đã mở Gmail trên máy của bạn rồi"

### Hướng kết nối: buddy → lamp

**Buddy là WS client. Lamp là WS server.** Lý do:

- Buddy không cần mở port nào. Firewall Mac không bị động chạm. Attack surface thấp.
- Lamp đã có hostname mDNS ổn định (`lamp-xxxx.local`) theo `project_mdns_hostname.md`. Buddy resolve và connect.
- WS persistent duy nhất → latency command = 1 round-trip (không có cold-start TCP/TLS mỗi command).
- Logic reconnect nằm ở buddy (đơn giản — buddy phát hiện lamp reboot và auto reconnect).

**Phương án thay thế đã cân nhắc:** lamp connect tới buddy. Bỏ vì:
- Phải mở port trên Mac (firewall prompt mỗi lần)
- Buddy phải mDNS-advertise cho lamp thấy (thêm complexity)
- HTTP layer của lamp đã là integration surface; giữ consistent

---

## 4. Component

### 4.1 `lumi-buddy` (app Swift macOS)

- `NSStatusItem` trên menu bar (không có Dock icon; `setActivationPolicy(.accessory)`)
- Menu: trạng thái pair, trạng thái kết nối, command gần nhất, "Pause", "Revoke pairing", "Quit"
- mDNS browser qua `Network.framework` (`NWBrowser`)
- Luồng pairing (6-digit code, token Keychain)
- `URLSessionWebSocketTask` persistent tới lamp
- Command executor:
  - **NSWorkspace** để launch app, mở URL
  - **CGEvent** để gõ phím & key combo
  - **AppleScript / OSAScript** cho "close app" và vài script trắng-list
  - **UNUserNotificationCenter** cho notification desktop
- Helper permission (prompt Accessibility, Automation per-app)
- Audit log local (`~/Library/Application Support/LampBuddy/audit.log`) — đồng thời push lên lamp khi có thể
- OSLog (unified logging) cho debug

### 4.2 `lumi` Go server — package mới `internal/buddy/`

| File | Trách nhiệm |
|------|-------------|
| `types.go` | Schema command / response, type pairing |
| `store.go` | Lưu paired buddy (token, fingerprint, name) — vào `config/buddies.json` |
| `pairing.go` | Sinh code, validate, cấp token |
| `registry.go` | Registry in-memory các buddy đang connect (WS handle) |
| `ws.go` | Handler WS upgrade, auth, vòng lặp message cho từng buddy |
| `dispatcher.go` | `Dispatch(buddyID, cmd) → response` — req/reply qua WS với timeout |
| `service.go` | Service tầng cao gộp các phần trên |
| `wire.go` | Wire provider set của Google |

Route HTTP mới (`server/buddy/delivery/http/`):

| Route | Auth | Mục đích |
|-------|------|----------|
| `POST /api/buddy/pair/start` | admin | Cấp code 6-digit, hết hạn 60s |
| `POST /api/buddy/pair/confirm` | code | Xác nhận code → trả token long-lived |
| `GET  /api/buddy/list` | admin | Liệt kê buddy đã pair + status |
| `DELETE /api/buddy/:id` | admin | Hủy pairing |
| `GET  /api/buddy/ws` | bearer token | WS upgrade cho buddy |
| `POST /api/buddy/command` | internal/admin | Dispatch 1 command (OpenClaw skill gọi) |
| `GET  /api/buddy/status` | admin | Tổng quan trạng thái kết nối |
| `GET  /api/buddy/audit` | admin | Audit log (paginated) |

### 4.3 `lelamp` (Python) — **không sửa cho MVP**

Hardware-only theo `feedback_lelamp_external.md`. STT → OpenClaw, OpenClaw → TTS đã có sẵn. Luồng buddy chỉ chạm tầng skill của OpenClaw — tầng đó nằm trong skill directory của OpenClaw, không phải Python source của lelamp.

### 4.4 OpenClaw skill: `computer-use`

- Nằm trong skill directory của OpenClaw (path tùy convention OpenClaw)
- `SKILL.md` mô tả trigger và tool surface
- Trigger pattern gồm tiếng Việt ("mở ... trên máy tính", "vào trang ... trên máy", "đóng app ...") và tiếng Anh ("open ... on my computer", "go to ... on my mac")
- Skill script (hoặc tool-call LLM) build command rồi `curl` `http://localhost:5000/api/buddy/command` với internal auth header
- Trả về kết quả TTS-friendly ("đã mở Chrome rồi", "không tìm thấy máy tính đã pair", …)

### 4.5 Web UI (`lumi/web/`)

Page mới `Paired Computers`:
- Liệt kê buddy đã pair (tên, OS, last seen, status)
- Nút "Add new" → gọi `/api/buddy/pair/start` → hiển thị code 6-digit → poll `/api/buddy/list` để phát hiện pair thành công
- Nút "Revoke" trên mỗi row

---

## 5. Schema command v1

### Request (lamp → buddy)

```json
{
  "id": "uuid-v4",
  "action": "open_app",
  "params": { "app": "Google Chrome" },
  "timeout_ms": 5000,
  "issued_at": "2026-05-21T10:00:00Z",
  "issued_by": "computer-use:openclaw"
}
```

### Response (buddy → lamp)

```json
{
  "id": "uuid-v4",
  "ok": true,
  "result": { "pid": 1234 },
  "error": null,
  "duration_ms": 412
}
```

### Action MVP

| Action | Params | Executor |
|--------|--------|----------|
| `open_app` | `{app: string}` | `NSWorkspace.shared.launchApplication` |
| `close_app` | `{app: string, force?: bool}` | AppleScript `tell application "X" to quit` |
| `open_url` | `{url: string, browser?: "default"\|"chrome"\|"safari"}` | `NSWorkspace.shared.open(URL)` |
| `type_text` | `{text: string, delay_ms?: int}` | `CGEventKeyboardSetUnicodeString` |
| `key_combo` | `{keys: ["cmd","n"]}` | `CGEventCreateKeyboardEvent` với modifier |
| `notification` | `{title: string, body?: string}` | `UNUserNotificationCenter` |
| `ping` | `{}` | health-check, trả `{ok:true}` |

Định nghĩa sẵn nhưng để sau:
- `applescript` (chỉ whitelist)
- `screenshot` (phase vision)
- `click_at` (phase vision)
- `read_clipboard` / `write_clipboard`
- `focus_window` / `bring_to_front`

---

## 6. Discovery & pairing

### Discovery (mỗi lần buddy khởi động)

- Buddy duyệt `_lumi._tcp.local` qua `NWBrowser`
- Mỗi service tìm thấy: resolve hostname → lưu vào danh sách
- MVP: giả định chỉ có 1 lamp trên LAN → auto chọn cái đầu
- Fallback: nhập hostname thủ công (`lamp-xxxx.local`) trong menu

### Pairing (1 lần)

1. User mở menu buddy → "Pair with Lamp" → buddy gọi lamp `POST /api/buddy/pair/start` (anonymous; rate-limited)
2. Lamp sinh code 6-digit, hiện trong web UI `/devices` (hoặc nơi nào đó); cũng trả code trong response để buddy có thể hướng dẫn
3. Lamp giữ code trong RAM 60s
4. User đọc code từ web UI lamp / display
5. User nhập code vào buddy
6. Buddy gọi `POST /api/buddy/pair/confirm {code, name, fingerprint, os_version}`
7. Lamp validate code, sinh bearer token long-lived, lưu `{token, fingerprint, name, created_at}` vào `buddies.json`
8. Buddy lưu token vào macOS Keychain (service `network.autonomous.ai.lamp-buddy`)
9. Buddy mở WS với `Authorization: Bearer <token>`

### Reconnect

- Buddy giữ WS mở + ping định kỳ (15s/lần)
- Khi disconnect: backoff exponential (1s, 2s, 4s, … cap 30s)
- Khi lamp reboot: lamp thấy token cũ trong `buddies.json` và accept WS ngay (token persistent)
- Khi revoke: lamp xóa khỏi `buddies.json`, từ chối với HTTP 401 → buddy phát hiện, drop session, prompt re-pair

---

## 7. Security model

| Layer | Cơ chế |
|-------|--------|
| Pairing | Yêu cầu user thao tác thực sự trên Mac (gõ code). Không thể pair lén. |
| Lưu token | macOS Keychain (encrypted at rest) bên buddy; `buddies.json` bên lamp (file mode 0600) |
| Transport | WS over HTTP trên LAN (MVP). **TLS để sau** — risk được document, mitigate bằng LAN-only + pairing. v1.1: self-signed cert + pinning. |
| Indicator session | Chấm đỏ trên menu bar khi buddy đang connect + active. Luôn nhìn thấy. |
| Audit log | Mọi command đều log (timestamp, action, hash params, source). File local + push lên lamp `/api/buddy/audit`. |
| Kill switch | "Pause" trên menu = drop WS giữ token. "Revoke pairing" = drop token + bảo lamp xóa. |
| Permission gating | Command fail clean với error rõ ràng nếu permission macOS bị deny (không silent fail). |
| Blast radius | **Document rõ với user**: Lamp có quyền tương đương account user trên Mac. Trust ask là vấn đề UX trung tâm. |
| Rate limit | Lamp side: max N command/sec/buddy. Tránh runaway loop. |

### Threat đã cân nhắc

1. **Attacker trên LAN** → không pair được nếu không có code từ web UI. Không replay token được nếu không phá Keychain.
2. **Lamp bị compromise** → chạy được command tùy ý trên Mac (= blast radius). Mitigation: user revoke bất cứ lúc nào từ menu bar, không cần truy cập lamp.
3. **Buddy bị compromise** (malware Mac hijack WS) → có thể gửi response giả về lamp. Mitigation: command ID + signed response (v1.1+).
4. **Eavesdrop trên LAN** → MVP không mã hóa WS. Chấp nhận với LAN nhà, phải fix trước khi deploy network không tin cậy.

---

## 8. macOS permission

| Permission | Vì sao | Khi nào trigger | Nếu deny |
|------------|--------|-----------------|----------|
| Accessibility | `CGEvent` inject input (type_text, key_combo) | Command `type_text` / `key_combo` đầu tiên | Command trả error; menu bar hiện "⚠ Cần Accessibility" |
| Automation (per-app) | AppleScript điều khiển từng app (close_app) | Lần đầu close_app cho từng app target | Command trả error |
| Notifications | Hiện trạng thái buddy & action noti | Lần đầu app launch (request ngay) | Action `notification` fail; indicator status vẫn chạy |

**Không cần Screen Recording** ở MVP (không screenshot).

---

## 9. Roadmap tương lai

| Version | Feature | Ghi chú |
|---------|---------|---------|
| v1.1 | Vision fallback qua Claude Computer Use | Cho command không match intent |
| v1.1 | TLS cho WS | Self-signed + cert pinning |
| v1.2 | Port Windows | Khả năng cao là Tauri/Rust |
| v1.3 | Port Linux (X11 trước) | Wayland để sau |
| v1.4 | Nhiều buddy trên 1 lamp | Kịch bản gia đình |
| v1.5 | Screen Recording + screenshot on-demand | Lamp request frame |
| v2.0 | Code signing + notarization + Sparkle | Distribution thực sự |
| v2.0 | Helper riêng theo app | Tích hợp first-party: Chrome extension, Slack bot, … |

---

## 10. Risk & câu hỏi mở

1. **Trust** — vấn đề UX trung tâm. Cần indicator session rõ ràng, truy cập audit log, kill switch dễ.
2. **macOS API churn** — Accessibility / AppleScript API hay thay đổi giữa các major version. Cần pin OS tối thiểu và test với version mới khi release.
3. **Latency** — voice → STT → OpenClaw → intent → WS → buddy → action ≈ 2–4s end-to-end. Sẽ thấy chậm so với Siri. Mitigation: TTS feedback ("ok, đang mở Chrome…") che lag.
4. **Distribution không notarize** — lần chạy đầu phải right-click → Open. Chấp nhận cho MVP. Plan ký ở v2.0.
5. **Command nhập nhằng** — "mở app" thiếu target cần TTS clarify lại. Skill phải xử lý.
6. **Mac sleep / khóa máy** — buddy không drive được nếu màn khóa. Định behavior: queue + retry hay error? MVP: error với message rõ ràng.
7. **Nhiều Mac trên cùng LAN** — nhiều buddy, 1 lamp. MVP cho phép 1 buddy đã pair. Document rõ.
8. **Lamp khác mạng** — buddy không connect được. Để khi nào có VPN/relay phase.

---

## 11. Quyết định tech stack

| Quyết định | Chọn | Lý do |
|------------|------|-------|
| Ngôn ngữ buddy | **Swift (native)** | API Mac tốt nhất (CGEvent, AppleScript, NSWorkspace đều native); binary nhỏ nhất; không cần lớp plugin |
| Project format | **Swift Package Manager** lúc đầu | Không cần Xcode; iterate nhanh. Chuyển Xcode `.xcodeproj` khi cần `.app` bundle + signing |
| Min macOS | **macOS 13 (Ventura)** | API `NWBrowser` modern, `Network.framework` chín; cũ hơn phải backport |
| Network protocol | **WebSocket (raw, không TLS MVP)** | Persistent, hai chiều, support tốt qua URLSessionWebSocketTask. TLS ở v1.1 |
| Discovery | **mDNS qua `Network.framework` `NWBrowser`** | Native, không cần dep thứ 3 |
| Codec command | **JSON** | Đồng style với serializer của lamp; dễ debug |
| Lưu token | **macOS Keychain** | Encrypted, OS quản lý |
| Lưu pref | **UserDefaults / plist** | Chuẩn macOS cho non-secret |
| Logging | **OSLog (unified)** | Native, xem được trong Console.app |
| Test framework | **XCTest** | Chuẩn |
| Go side | **Pattern hiện có** (Gin, Wire, internal/) | Theo `internal/openclaw/`, `server/<domain>/delivery/http/` |
| Vị trí folder lumi-buddy | **Root repo** cạnh `lumi/`, `lelamp/` | Self-contained, build độc lập |

---

## 12. Discussion log (2026-05-21)

### Brief ban đầu của Leo

> Mình muốn làm 1 chức năng remote computer use. Tưởng tượng Lumi lamp sẽ remote vào MacBook của mình, điều khiển máy tính, app, browser Chrome … giống cách supporter qua TeamViewer điều khiển máy của mình.
>
> Máy MacBook cần chạy 1 software lumi-buddy trên top status bar, sau đó quét xem có Lumi lamp đang chạy LAN IP gì đó, thì accept cho Lumi lamp được connect và computer use máy MacBook.
>
> VD mình nói "mở Chrome trên máy tính và vào Gmail lấy mail ra", hay "tự động join Google Meet" … cần code thêm 1 folder riêng `lumi-buddy` (chưa rõ dùng ngôn ngữ gì swift hay flutter …). Sau đó install vào Mac/Windows/Linux. Software này paring với Lumi lamp và cho phép Lumi lamp điều khiển.

### Các phương án đã cân nhắc

**1. Dùng protocol có sẵn (VNC, RustDesk, TeamViewer)**
- Pro: không phải code
- Con: các app này hướng đến controller là người, không phải AI/tool-API. Lumi vẫn phải "nhìn màn hình + click/type" qua chúng → tương đương build TeamViewer client trong Lumi.
- **Bỏ.**

**2. Screen Sharing / VNC built-in của macOS**
- Pro: không cần cài thêm gì trên Mac
- Con: giống trên — thiết kế cho controller là người. Gắn vision-action loop của Lumi lên trên rất gượng.
- **Bỏ cho MVP.**

**3. Mac Companion riêng (chọn)**
- Pro: tiếp cận API native, latency nhỏ nhất, command intent-level mà không cần vision-loop, có thể tích hợp sâu macOS (Spotlight, Shortcuts, …)
- Con: phải build & maintain. Code per-platform (về sau).
- **Chọn.**

### Quyết định: intent-based vs vision-loop

**Intent A (chọn):** parse voice intent → map sang command structured (`open_app`, `type_text`) → buddy chạy local, không screenshot.
- Latency: <500ms/command
- Cost: 0 LLM call thêm (chỉ dùng intent parsing có sẵn của OpenClaw)
- Robustness: giòn với request lạ
- Coverage: 80% use case đã nêu (mở Chrome, vào Gmail, join Meet, type, close app)

**Vision B (để v1.1):** screenshot → Claude Computer Use API → click(x,y) → lặp.
- Latency: 3–10s/turn
- Cost: mỗi screenshot là message to (đắt)
- Robustness: bền — Claude tự nhìn và thích nghi
- Coverage: bất cứ thứ gì hiện trên màn hình

**Hybrid C (để v1.1):** A mặc định, fallback B cho command không nhận diện được.

### Quyết định: LLM chạy ở đâu?

**Option a (chọn):** OpenClaw (trên lamp) parse intent. Buddy là dumb executor nhận command structured.

**Option b (để sau):** Buddy host LLM riêng cho task phức tạp ("đọc email mới nhất tóm tắt"). Để đến khi Vision B cần.

### Quyết định: ngôn ngữ

Mac-only MVP → **Swift native**. Tauri/Rust để phase Windows/Linux. Flutter bỏ (bridge native cho input/screen yếu). Electron bỏ (overhead RAM không chấp nhận với app menu-bar thường trú).

### Quyết định: hướng kết nối

**Buddy → lamp** (chọn) — xem §3 cho lý do. Buddy là WS client, lamp là WS server.

### Quyết định: distribution (MVP)

**Không sign** — user right-click → Open ở lần chạy đầu. Apple Developer account ($99/năm) để v2.0.

### Quyết định với `lelamp` và `lumi` Go

- `lelamp` (Python) — **không sửa**. Chỉ hardware.
- `lumi` (Go) — **package mới** `internal/buddy/`, route HTTP mới, WS gateway mới.
- `OpenClaw` — skill mới `computer-use`.
- `lumi/web` — page mới "Paired Computers".
- `CLAUDE.md` — thêm row docs.

---

## 13. Tham chiếu

- `project_mdns_hostname.md` — lamp publish `lamp-<last4hex>.local`
- `feedback_lelamp_external.md` — hardware code ở Python, không phải Go
- `project_security_login_ui_batch.md` — security audit mới đóng; pattern cookie HMAC + bcrypt admin có thể reuse cho auth buddy
- [Tài liệu Anthropic Computer Use](https://docs.anthropic.com/en/docs/build-with-claude/computer-use) — cho phase vision v1.1
- [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit) — cho khả năng screenshot v1.5
- [Apple Accessibility API](https://developer.apple.com/documentation/applicationservices/axuielement_h) — để target window/button
