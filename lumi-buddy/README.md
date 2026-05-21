# Lumi Buddy

Native companion apps that let a Lumi lamp control your computer via voice (open apps, navigate browser, type, etc.) — TeamViewer-style remote control, but driven by AI through the lamp.

**Status:** Phase 1A — Mac-only scaffold. Menu bar shell that runs but does no networking yet.

**Design doc:** [`docs/lumi-buddy.md`](docs/lumi-buddy.md) · [VI](docs/vi/lumi-buddy_vi.md)
**MVP plan:** [`docs/lumi-buddy-mvp.md`](docs/lumi-buddy-mvp.md) · [VI](docs/vi/lumi-buddy-mvp_vi.md)

---

## Platforms

| Platform | Status | Folder |
|----------|--------|--------|
| **macOS 13+** | Phase 1A scaffold | [`macos/`](macos/) |
| Windows | Planned v1.2 (likely Tauri/Rust) | — |
| Linux (X11) | Planned v1.3 | — |

The MVP targets macOS only. Each platform lives in its own subfolder so toolchains don't leak between them. Cross-platform glue (protocol schemas, command formats) is captured in [`docs/lumi-buddy.md`](docs/lumi-buddy.md) so future ports stay aligned.

---

## macOS — quick start

Requirements: macOS 13 (Ventura)+, Swift 5.9+ (Xcode 15 or Command Line Tools).

The `Makefile` at this directory wraps everything. From `lumi-buddy/`:

```bash
make help       # list all targets
make run        # dev — runs in foreground (Ctrl-C to stop)
make app        # build dist/LumiBuddy.app
make open       # launch the bundled .app
make install    # copy bundled .app to /Applications
make audit      # tail the audit log
make kill       # stop any running LumiBuddy
make clean      # remove all build artifacts
make mock       # run mock-lamp (Go) — test buddy without real lumi side
```

Behind the scenes `make run` calls `swift run` inside `macos/`. Use it if you don't want to remember the SPM commands.

### First launch (Gatekeeper)

No code signing yet. First time launching the bundled `.app` Gatekeeper will block it:

1. `make app` to build `dist/LumiBuddy.app`
2. Finder → right-click the `.app` → **Open** → confirm in the dialog
3. Subsequent launches work normally (`make open` or double-click)

Apple Developer signing + notarization comes in v2.0.

## Testing end-to-end with mock-lamp

Until the real lumi Go side lands, you can drive the buddy from a tiny Go mock server. See [`mock-lamp/README.md`](mock-lamp/README.md). Two terminals:

```bash
# Terminal 1
make run        # buddy menu bar app

# Terminal 2
make mock       # prints pairing code, drops you into a REPL
```

Then in buddy's menu → **Pair with Lumi…** → host `localhost:8765` + the 6-digit code. The mock's REPL sends commands (`ping`, `open_app Calculator`, `type_text hello`, etc.) over the WebSocket.

---

## Folder layout

```
lumi-buddy/
├── README.md           # this file
├── .gitignore
├── docs/               # design + MVP plan (EN + VI)
│   ├── lumi-buddy.md
│   ├── lumi-buddy-mvp.md
│   └── vi/
└── macos/              # macOS native (Swift) — current MVP target
    ├── Package.swift
    └── Sources/LumiBuddy/
        ├── main.swift
        ├── AppDelegate.swift
        ├── MenuBarController.swift
        ├── Discovery/   # Phase 1B — mDNS lamp discovery
        ├── Pairing/     # Phase 1C — 6-digit pairing + Keychain
        ├── Connection/  # Phase 1D — WebSocket to lamp
        ├── Commands/    # Phase 1E — command dispatcher + executors
        ├── Permissions/ # macOS permission helpers
        └── Audit/       # local audit log
```

---

## What works (Phase 1A)

- Status bar icon (💡)
- Menu with "Pair with Lumi…", "About", "Quit"
- Accessory activation policy (no Dock icon)

## What does NOT work yet

- Lamp discovery (Phase 1B)
- Pairing flow (Phase 1C)
- WebSocket to lamp (Phase 1D)
- Command execution (Phase 1E)

Each phase ships as a separate PR. See [`docs/lumi-buddy-mvp.md`](docs/lumi-buddy-mvp.md) for the full breakdown.

---

## Comments policy

English only — see project `CLAUDE.md`.

## License

Same as the parent repository.
