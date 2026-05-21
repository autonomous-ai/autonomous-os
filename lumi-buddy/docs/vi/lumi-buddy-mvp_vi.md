# Lumi Buddy MVP — Kế hoạch implement

> **Trạng thái:** Sẵn sàng execute
> **Cập nhật:** 2026-05-21
> **Design doc:** [lumi-buddy_vi.md](./lumi-buddy_vi.md)
> **Mục tiêu hoàn thành:** ~2 tuần (1 dev)

Đây là plan action cho **MVP của Lumi Buddy** — app companion macOS cho phép Lumi lamp điều khiển máy tính qua voice. Lý do thiết kế đầy đủ ở [lumi-buddy_vi.md](./lumi-buddy_vi.md). Doc này liệt kê *build cái gì, thứ tự nào, accept ra sao*.

---

## Scope

**Trong scope:**
- macOS-only (macOS 13+)
- Swift Package Manager project ở `lumi-buddy/`
- Menu bar app (`NSStatusItem`, không có Dock icon)
- mDNS discovery lamp trên LAN
- Pairing 6-digit (web UI lamp hiện code)
- WS connection persistent (`buddy → lamp`)
- Command executor: `open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`
- Lumi Go: package `internal/buddy/` + 7 HTTP route + WS gateway
- OpenClaw skill `computer-use` (intent → command cơ bản)
- Web UI: page "Paired Computers" ở `lumi/web/`
- Audit log (backend file only — chưa có UI ở MVP)

**Ngoài scope (chờ sau MVP):**
- Command vision / screenshot
- AppleScript executor ngoài `close_app` đơn giản
- Port Windows / Linux
- Code signing / notarization (right-click → Open là cách cài đặt)
- Sparkle / auto-update
- TLS cho WS (LAN-only + pairing được xem là đủ cho self-hosted MVP)
- Nhiều buddy trên 1 lamp
- UI audit log
- UI rate limit
- Push lamp restart cho buddy
- Monitoring resource của buddy

---

## Các phase

Mỗi phase ship & review độc lập được.

### Phase 1A — Folder + scaffold Swift

**Files:**
- `lumi-buddy/README.md`
- `lumi-buddy/macos/Package.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/main.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/AppDelegate.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/MenuBarController.swift`
- `lumi-buddy/.gitignore`

**Acceptance:** `cd lumi-buddy/macos && swift run` hiện icon trên status bar. Menu có "About Lumi Buddy", "Quit". Không crash. Activation policy là `.accessory` (không có Dock icon).

### Phase 1B — Discovery lamp (mDNS)

**Files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Discovery/LampDiscovery.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Discovery/LampInfo.swift`
- Update `MenuBarController.swift` để hiện lamp tìm thấy

**Acceptance:** Khi lamp đang chạy trên LAN (advertise `_lumi._tcp.local`), menu buddy hiện ví dụ `lumi-a1b2.local — 192.168.1.50` như item bấm được. Cũng có: option nhập hostname thủ công.

> Note: confirm tên service mDNS hiện có của lamp. Hiện publish `lumi-<last4hex>.local`; có thể cần advertise thêm service `_lumi._tcp.local` cho browsable. Có thể cần sửa nhỏ bên lelamp/lumi (xem §lamp-side dưới).

### Phase 1C — Luồng pairing

**File buddy:**
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingManager.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingStore.swift` (Keychain)
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingWindow.swift` (UI nhập code)

**File Lumi Go:**
- `lumi/internal/buddy/types.go`
- `lumi/internal/buddy/store.go`
- `lumi/internal/buddy/pairing.go`
- `lumi/internal/buddy/service.go`
- `lumi/server/buddy/delivery/http/handler.go`
- `lumi/server/buddy/delivery/http/handler_pair.go`
- `lumi/internal/buddy/wire.go`
- Sửa: `lumi/server/server.go` (đăng ký route)
- Sửa: `lumi/server/wire.go` (provider)
- Chạy: `make generate`

**File Lumi web:**
- `lumi/web/src/pages/PairedComputers.tsx` (sơ — chỉ hiện code)
- Update `lumi/web/src/App.tsx` (route)
- Update `lumi/web/src/lib/api.ts` (endpoint pair)

**Route thêm:**
- `POST /api/buddy/pair/start`
- `POST /api/buddy/pair/confirm`
- `GET  /api/buddy/list`
- `DELETE /api/buddy/:id`

**Acceptance:**
1. User mở menu buddy → "Pair with Lumi" → web UI lamp hiện code 6-digit
2. User đọc code, gõ vào cửa sổ nhập code của buddy
3. Buddy lưu token vào Keychain
4. Lamp persist buddy vào `buddies.json`
5. Menu buddy hiện "Paired with lumi-xxxx"
6. `GET /api/buddy/list` trả về buddy đã pair

### Phase 1D — WebSocket connection

**File buddy:**
- `lumi-buddy/macos/Sources/LumiBuddy/Connection/LumiConnection.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Connection/Reconnect.swift`

**File Lumi Go:**
- `lumi/internal/buddy/registry.go`
- `lumi/internal/buddy/ws.go`
- `lumi/server/buddy/delivery/http/handler_ws.go`
- Update: `lumi/server/server.go` (đăng ký route WS)

**Route thêm:**
- `GET /api/buddy/ws` (WS upgrade)
- `GET /api/buddy/status`

**Acceptance:**
- Buddy auto-connect WS khi khởi động (và sau pair)
- Lamp log `[buddy] connected: <fingerprint>` khi connect
- Menu buddy hiện chấm xanh khi connected, đỏ khi disconnected
- WS sống qua lamp reboot (buddy reconnect với backoff)
- `GET /api/buddy/status` trả về `{"connected": [...], "paired": [...]}`

### Phase 1E — Command executor (bên buddy)

**Files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Command.swift` (type)
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/CommandDispatcher.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/AppExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/URLExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/KeyboardExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/NotificationExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/PingExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Permissions/AccessibilityCheck.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Audit/AuditLog.swift`

**Acceptance:**
- WS nhận command JSON → dispatcher decode → executor chạy → trả response JSON
- Đủ các action MVP (`open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`)
- Permission deny trả error sạch (không crash)
- File audit log ghi vào `~/Library/Application Support/LumiBuddy/audit.log`

### Phase 1F — Dispatch command (bên lumi Go)

**Files:**
- `lumi/internal/buddy/dispatcher.go`
- `lumi/server/buddy/delivery/http/handler_command.go`
- Update: wire provider, chạy `make generate`

**Route thêm:**
- `POST /api/buddy/command`

**Acceptance:**
- `curl -X POST http://lamp/api/buddy/command -H 'Authorization: Bearer <admin-token>' -d '{"action":"ping"}'` trả về `{"ok":true,"result":{"pong":true}}`
- Timeout chạy (default 5s; 503 nếu buddy không response)
- 404 nếu không có buddy connect
- Command concurrent xử lý đúng (match response theo command ID)

### Phase 1G — OpenClaw skill

**Files (vị trí tùy convention skill của OpenClaw):**
- `computer-use/SKILL.md`
- `computer-use/script.sh` (hoặc tương đương)

**Acceptance:**
- User nói với lamp: "Mở Chrome trên máy tính" → buddy launch Chrome → lamp đọc "đã mở Chrome rồi"
- User nói: "Vào Gmail trên máy" → buddy mở gmail.com
- User nói: "Join Google Meet" → buddy mở URL meet đã config (TBD — config)
- Skill xử lý gracefully "chưa pair buddy nào" ("chưa có máy tính nào kết nối")

### Phase 1H — Hoàn thiện web UI

**Files:**
- Update `lumi/web/src/pages/PairedComputers.tsx`
- Update `lumi/web/src/components/` nếu cần

**Acceptance:**
- Page list buddy đã pair với tên, OS, last seen, online/offline
- Nút "Add new" bắt đầu pairing, hiện code 6-digit có countdown
- Nút "Revoke" cho từng row (lamp xóa; buddy nhận 401 → drop session)
- Indicator visual khi có command đang in flight

### Phase 1I — Docs + dọn dẹp

**Files:**
- Verify `docs/lumi-buddy.md` match implementation thực (update nếu drift)
- Verify `docs/vi/lumi-buddy_vi.md` match
- Thêm `lumi-buddy/README.md` instruction build
- Update `CLAUDE.md` root: row doc table cho lumi-buddy
- Update `Makefile` top-level: target `build-buddy`
- Thêm file `VERSION_BUDDY` ở root → `0.0.1`
- Bump `VERSION_LUMI`, `VERSION_WEB` nếu cần

**Acceptance:**
- Dev mới clone về có thể `cd lumi-buddy/macos && swift run` và follow README để pair với lamp
- CLAUDE.md doc table có row mới
- `make build-buddy` cho ra `lumi-buddy/.build/release/LumiBuddy`

---

## Lamp-side cần verify trước Phase 1B

1. **mDNS browsability** — confirm lamp publish `_lumi._tcp.local` cho `NWBrowser`. Nếu chỉ có host record `lumi-xxxx.local`, cần thêm service publishing (chắc trong `lumi` startup hoặc avahi config).
2. **Convention header admin auth** — confirm endpoint buddy mới dùng `Authorization: Bearer <token>` (cookie hay bearer); reuse pattern `project_security_login_ui_batch.md`.
3. **Vị trí OpenClaw skill** — tìm xem skill đang sống ở đâu, naming convention, lamp đăng ký skill thế nào. (Có thể trong filesystem lamp `~/.openclaw/skills/<name>/SKILL.md`.)

---

## File inventory (trạng thái cuối MVP)

### Swift (`lumi-buddy/macos/`)
```
lumi-buddy/
├── README.md
├── .gitignore
├── docs/                          # design + MVP plan (EN + VI)
└── macos/
    ├── Package.swift
    └── Sources/LumiBuddy/
        ├── main.swift
        ├── AppDelegate.swift
        ├── MenuBarController.swift
        ├── Discovery/
        │   ├── LampDiscovery.swift
        │   └── LampInfo.swift
        ├── Pairing/
        │   ├── PairingManager.swift
        │   ├── PairingStore.swift
        │   └── PairingWindow.swift
        ├── Connection/
        │   ├── LumiConnection.swift
        │   └── Reconnect.swift
        ├── Commands/
        │   ├── Command.swift
        │   ├── CommandDispatcher.swift
        │   └── Executors/
        │       ├── AppExecutor.swift
        │       ├── URLExecutor.swift
        │       ├── KeyboardExecutor.swift
        │       ├── NotificationExecutor.swift
        │       └── PingExecutor.swift
        ├── Permissions/
        │   └── AccessibilityCheck.swift
        └── Audit/
            └── AuditLog.swift
```

Subfolder `lumi-buddy/windows/` và `lumi-buddy/linux/` sẽ host port tương lai (v1.2+). Mỗi platform self-contained để toolchain không "lây" lẫn nhau.

### Go (`lumi/`)
```
lumi/internal/buddy/
├── types.go
├── store.go
├── pairing.go
├── registry.go
├── ws.go
├── dispatcher.go
├── service.go
└── wire.go

lumi/server/buddy/delivery/http/
├── handler.go
├── handler_pair.go
├── handler_ws.go
└── handler_command.go
```

Sửa:
- `lumi/server/server.go` (đăng ký route)
- `lumi/server/wire.go` (provider set)
- `lumi/server/wire_gen.go` (regenerated)

### Web (`lumi/web/`)
```
lumi/web/src/
├── pages/PairedComputers.tsx (mới)
├── App.tsx (sửa — thêm route)
└── lib/api.ts (sửa — thêm endpoint buddy)
```

### OpenClaw skill
```
<openclaw-skills-dir>/computer-use/
├── SKILL.md
└── script.sh (hoặc tương đương)
```

### Khác
- `CLAUDE.md` — thêm row doc table
- `Makefile` — target `build-buddy`
- `VERSION_BUDDY` (root) — `0.0.1`

---

## Test end-to-end

1. Mac boot, user start `lumi-buddy.app` (hoặc `swift run` cho dev)
2. Lumi lamp đang chạy trên LAN
3. Menu buddy hiện `lumi-xxxx.local` đã tìm thấy
4. User click "Pair with Lumi" → web UI lamp hiện code 6-digit
5. User gõ code vào buddy → "Paired ✓"
6. Menu buddy hiện "Connected to lumi-xxxx" với chấm xanh
7. User nói với lamp: "Mở Chrome trên máy tính của tôi"
8. Lamp dispatch command qua WS
9. Chrome launch trên Mac
10. Lamp đọc: "Đã mở Chrome trên máy bạn rồi"
11. User nói: "Vào Gmail" → Chrome navigate gmail.com
12. User nói: "Đóng Chrome" → Chrome quit
13. User mở menu buddy → "Pause" → command tiếp theo từ lamp trả "máy tính tạm dừng"
14. User "Resume" → command lại chạy được
15. User từ web UI lamp → "Revoke" → buddy nhận 401 → menu hiện "Unpaired"

---

## Cần confirm với Leo trước khi start

- [x] **MVP Mac-only** — confirmed
- [x] **Intent-based (A), không vision** — confirmed
- [x] **Build from scratch** (không fork Open Interpreter / Computer Use demo) — confirmed
- [x] **MVP không sign code** — right-click → Open OK — confirmed
- [ ] **Pairing model** — 1 lamp ↔ 1 buddy (MVP). Confirm? (reply của Leo gợi ý yes nhưng nên confirm)
- [ ] **"Join Google Meet" — URL cố định hay nhớ link gần nhất?** — MVP đề xuất URL config trong preferences của buddy (user set room họp định kỳ)
- [ ] **Vị trí skill directory của OpenClaw** — cần tìm xem skill hiện sống ở đâu trong repo này
- [ ] **Versioning** — `VERSION_BUDDY` follow scheme `VERSION_LUMI`?

---

## Risk riêng của MVP

1. **Publishing mDNS service** — nếu lamp chưa publish `_lumi._tcp.local` (chỉ host record), buddy không browse được nếu không sửa nhỏ bên lamp.
2. **Convention skill OpenClaw** — chưa biết cho đến khi inspect. Có thể ảnh hưởng design phase 1G.
3. **UX permission lần chạy đầu** — Accessibility prompt 1 lần; nếu user deny mà mình không re-prompt sạch, action keyboard fail âm thầm. Cần UX fallback.
4. **WS keepalive qua Mac sleep** — Mac sleep kill WS. Reconnect phải xử lý gracefully.
5. **Bundling** — `swift run` chạy dev OK nhưng install production cần `.app` bundle có `Info.plist`. Có thể để sau nhưng phải document.
