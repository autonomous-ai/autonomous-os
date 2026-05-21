# Lumi Buddy MVP — Implementation Plan

> **Status:** Ready to execute
> **Last updated:** 2026-05-21
> **Design doc:** [lumi-buddy.md](./lumi-buddy.md)
> **Target completion:** ~2 weeks (single dev)

This is the actionable plan for **MVP of Lumi Buddy** — the macOS companion app that lets Lumi lamp control the user's computer via voice. Full design rationale in [lumi-buddy.md](./lumi-buddy.md). This doc lists *what to build, in what order, with acceptance criteria*.

---

## Scope

**In scope:**
- macOS-only (macOS 13+)
- Swift Package Manager project at `lumi-buddy/`
- Menu bar app (`NSStatusItem`, no Dock icon)
- mDNS discovery of lamp on LAN
- 6-digit pairing flow (lamp web UI shows code)
- Persistent WS connection (`buddy → lamp`)
- Command executors: `open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`
- Lumi Go: `internal/buddy/` package + 7 HTTP routes + WS gateway
- OpenClaw skill `computer-use` (basic intent → command mapping)
- Web UI: "Paired Computers" page in `lumi/web/`
- Audit log (backend file only — no UI in MVP)

**Out of scope (defer to post-MVP):**
- Vision / screenshot commands
- AppleScript executor beyond simple `close_app`
- Windows / Linux ports
- Code signing / notarization (right-click → Open is the install method)
- Sparkle / auto-update
- TLS on WS (LAN-only + pairing seen as sufficient for self-hosted MVP)
- Multi-buddy per lamp
- Audit log UI
- Rate-limit UI
- Lamp restart push to buddy
- Buddy resource monitoring

---

## Phases

Each phase is independently shippable and reviewable.

### Phase 1A — Folder + Swift scaffold

**Files:**
- `lumi-buddy/README.md`
- `lumi-buddy/macos/Package.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/main.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/AppDelegate.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/MenuBarController.swift`
- `lumi-buddy/.gitignore`

**Acceptance:** `cd lumi-buddy/macos && swift run` shows a status bar icon. Menu has "About Lumi Buddy", "Quit". No crash. Process activation policy is `.accessory` (no Dock icon).

### Phase 1B — Lamp discovery (mDNS)

**Files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Discovery/LampDiscovery.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Discovery/LampInfo.swift`
- Update `MenuBarController.swift` to show discovered lamps

**Acceptance:** When a lamp is running on LAN (advertises `_lumi._tcp.local`), buddy menu shows e.g. `lumi-a1b2.local — 192.168.1.50` as a clickable item. Also: manual hostname entry option.

> Note: confirm lamp's existing mDNS service name. Currently it publishes `lumi-<last4hex>.local`; may need to also advertise a `_lumi._tcp.local` service for browsability. May require a small lelamp/lumi tweak (see lamp-side §1 below).

### Phase 1C — Pairing flow

**Buddy files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingManager.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingStore.swift` (Keychain)
- `lumi-buddy/macos/Sources/LumiBuddy/Pairing/PairingWindow.swift` (code entry UI)

**Lumi Go files:**
- `lumi/internal/buddy/types.go`
- `lumi/internal/buddy/store.go`
- `lumi/internal/buddy/pairing.go`
- `lumi/internal/buddy/service.go`
- `lumi/server/buddy/delivery/http/handler.go`
- `lumi/server/buddy/delivery/http/handler_pair.go`
- `lumi/internal/buddy/wire.go`
- Modify: `lumi/server/server.go` (register routes)
- Modify: `lumi/server/wire.go` (provider)
- Run: `make generate`

**Lumi web files:**
- `lumi/web/src/pages/PairedComputers.tsx` (initial — just code display)
- Update `lumi/web/src/App.tsx` (route)
- Update `lumi/web/src/lib/api.ts` (pair endpoints)

**Routes added:**
- `POST /api/buddy/pair/start`
- `POST /api/buddy/pair/confirm`
- `GET  /api/buddy/list`
- `DELETE /api/buddy/:id`

**Acceptance:**
1. User opens buddy menu → "Pair with Lumi" → web UI of lamp displays 6-digit code
2. User reads code, types into buddy code entry window
3. Buddy stores token in Keychain
4. Lamp persists buddy in `buddies.json`
5. Buddy menu now shows "Paired with lumi-xxxx"
6. `GET /api/buddy/list` returns paired buddy

### Phase 1D — WebSocket connection

**Buddy files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Connection/LumiConnection.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Connection/Reconnect.swift`

**Lumi Go files:**
- `lumi/internal/buddy/registry.go`
- `lumi/internal/buddy/ws.go`
- `lumi/server/buddy/delivery/http/handler_ws.go`
- Update: `lumi/server/server.go` (register WS route)

**Routes added:**
- `GET /api/buddy/ws` (WS upgrade)
- `GET /api/buddy/status`

**Acceptance:**
- Buddy auto-connects WS on startup (and after pairing)
- Lamp logs `[buddy] connected: <fingerprint>` on connect
- Buddy menu shows green dot when connected, red when disconnected
- WS survives lamp reboot (buddy reconnects with backoff)
- `GET /api/buddy/status` returns `{"connected": [...], "paired": [...]}`

### Phase 1E — Command executors (buddy side)

**Files:**
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Command.swift` (types)
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/CommandDispatcher.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/AppExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/URLExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/KeyboardExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/NotificationExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Commands/Executors/PingExecutor.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Permissions/AccessibilityCheck.swift`
- `lumi-buddy/macos/Sources/LumiBuddy/Audit/AuditLog.swift`

**Acceptance:**
- WS receives command JSON → dispatcher decodes → executor runs → response JSON returned
- All MVP actions implemented (`open_app`, `close_app`, `open_url`, `type_text`, `key_combo`, `notification`, `ping`)
- Permission denial returns clean error (not crash)
- Audit log file written to `~/Library/Application Support/LumiBuddy/audit.log`

### Phase 1F — Command dispatch (lumi Go side)

**Files:**
- `lumi/internal/buddy/dispatcher.go`
- `lumi/server/buddy/delivery/http/handler_command.go`
- Update: wire providers, run `make generate`

**Routes added:**
- `POST /api/buddy/command`

**Acceptance:**
- `curl -X POST http://lamp/api/buddy/command -H 'Authorization: Bearer <admin-token>' -d '{"action":"ping"}'` returns `{"ok":true,"result":{"pong":true}}`
- Timeout works (default 5s; 503 if buddy unresponsive)
- 404 if no buddy connected
- Concurrent commands handled (per-command ID matching for responses)

### Phase 1G — OpenClaw skill

**Files (location depends on OpenClaw skill conventions):**
- `computer-use/SKILL.md`
- `computer-use/script.sh` (or whatever scripting OpenClaw uses)

**Acceptance:**
- User says to lamp: "Mở Chrome trên máy tính" → buddy launches Chrome → lamp speaks "đã mở Chrome rồi"
- User says: "Vào Gmail trên máy" → buddy opens gmail.com
- User says: "Join Google Meet" → buddy opens last-used meet URL (TBD — config)
- Skill handles "no buddy paired" gracefully ("chưa có máy tính nào kết nối")

### Phase 1H — Web UI polish

**Files:**
- Update `lumi/web/src/pages/PairedComputers.tsx`
- Update `lumi/web/src/components/` as needed

**Acceptance:**
- Page lists paired buddies with name, OS, last seen, online/offline
- "Add new" button starts pairing flow, displays 6-digit code with countdown
- "Revoke" button per row works (lamp removes; buddy gets 401 → drops session)
- Visual indicator if a command is in flight

### Phase 1I — Docs + housekeeping

**Files:**
- Verify `docs/lumi-buddy.md` matches actual implementation (update if drift)
- Verify `docs/vi/lumi-buddy_vi.md` matches
- Add `lumi-buddy/README.md` build instructions
- Update root `CLAUDE.md`: doc table row for lumi-buddy
- Update top-level `Makefile`: `build-buddy` target
- Add `VERSION_BUDDY` file at root → `0.0.1`
- Bump `VERSION_LUMI`, `VERSION_WEB` as needed

**Acceptance:**
- Fresh-checkout dev can `cd lumi-buddy/macos && swift run` and follow README to pair with lamp
- CLAUDE.md doc table includes the new row
- `make build-buddy` produces `lumi-buddy/.build/release/LumiBuddy`

---

## Lamp-side prerequisites (verify before Phase 1B)

1. **mDNS browsability** — confirm lamp publishes `_lumi._tcp.local` for `NWBrowser`. If only `lumi-xxxx.local` host record exists, add service publishing (likely in `lumi` startup or avahi config).
2. **Admin auth header convention** — confirm whether new buddy endpoints should use `Authorization: Bearer <token>` (cookie or bearer); reuse `project_security_login_ui_batch.md` patterns.
3. **OpenClaw skill location** — find where existing skills live, naming convention, how lamp registers them. (Possibly in lamp's filesystem `~/.openclaw/skills/<name>/SKILL.md`.)

---

## File inventory (final state after MVP)

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

Subfolders `lumi-buddy/windows/` and `lumi-buddy/linux/` will host future ports (v1.2+). Each platform self-contained so toolchains don't cross-contaminate.

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

Modified:
- `lumi/server/server.go` (route registration)
- `lumi/server/wire.go` (provider set)
- `lumi/server/wire_gen.go` (regenerated)

### Web (`lumi/web/`)
```
lumi/web/src/
├── pages/PairedComputers.tsx (new)
├── App.tsx (modified — add route)
└── lib/api.ts (modified — add buddy endpoints)
```

### OpenClaw skill
```
<openclaw-skills-dir>/computer-use/
├── SKILL.md
└── script.sh (or equivalent)
```

### Other
- `CLAUDE.md` — doc table row added
- `Makefile` — `build-buddy` target
- `VERSION_BUDDY` (root) — `0.0.1`

---

## End-to-end acceptance test

1. Mac boots, user starts `lumi-buddy.app` (or `swift run` for dev)
2. Lumi lamp is running on LAN
3. Buddy menu shows `lumi-xxxx.local` discovered
4. User clicks "Pair with Lumi" → web UI on lamp displays 6-digit code
5. User types code into buddy → "Paired ✓"
6. Buddy menu shows "Connected to lumi-xxxx" with green dot
7. User says to lamp: "Mở Chrome trên máy tính của tôi"
8. Lamp dispatches command via WS
9. Chrome launches on Mac
10. Lamp speaks: "Đã mở Chrome trên máy bạn rồi"
11. User says: "Vào Gmail" → Chrome navigates to gmail.com
12. User says: "Đóng Chrome" → Chrome quits
13. User opens buddy menu → "Pause" → next command from lamp returns "máy tính tạm dừng"
14. User "Resume" → next command works again
15. User from lamp web UI → "Revoke" → buddy gets 401 → menu shows "Unpaired"

---

## Things to confirm with Leo before starting

- [x] **Mac-only MVP** — confirmed
- [x] **Intent-based (A), not vision** — confirmed
- [x] **Build from scratch** (not fork Open Interpreter / Computer Use demo) — confirmed
- [x] **No code signing for MVP** — right-click → Open OK — confirmed
- [ ] **Pairing model** — 1 lamp ↔ 1 buddy (MVP). Confirm? (Leo's reply implied yes, but worth confirming)
- [ ] **"Join Google Meet" — fixed URL or remembered last?** — for MVP, suggest a configurable URL in buddy preferences (so user can set their team's recurring meeting room)
- [ ] **OpenClaw skill directory location** — need to look up where existing skills live in this repo
- [ ] **Versioning** — should `VERSION_BUDDY` follow same scheme as `VERSION_LUMI`?

---

## Risks specific to MVP

1. **mDNS service publishing** — if lamp doesn't currently publish `_lumi._tcp.local` (only host record), buddy can't browse without a small lamp-side change.
2. **OpenClaw skill conventions** — unknown until inspected. May affect phase 1G design.
3. **Permission UX on first launch** — Accessibility prompt is one-shot; if user denies and we don't re-prompt cleanly, keyboard actions silently fail. Need fallback UX.
4. **WS keepalive across Mac sleep** — Mac sleep kills WS. Reconnect must handle gracefully.
5. **Bundling** — `swift run` works for dev but for production install we eventually need a `.app` bundle with `Info.plist`. Can defer but document the gap.
