# Autonomous Buddy

Native companion apps that let an Autonomous device control your computer via voice (open apps, navigate browser, type, etc.) — TeamViewer-style remote control, but driven by AI through the device.

**Status:** macOS companion with pairing, WebSocket command execution, Accessibility observations, and screenshot feedback. Computer-use checks and remaining live acceptance scenarios are documented below.

**Computer use:** [Device-driven desktop workflows](docs/computer-use.md) · [VI](docs/vi/computer-use_vi.md). Covers native UI observation, screenshot feedback, cancellation, and verification across apps.

**Design doc:** [`docs/autonomous-buddy.md`](docs/autonomous-buddy.md) · [VI](docs/vi/autonomous-buddy_vi.md)
**MVP plan:** [`docs/autonomous-buddy-mvp.md`](docs/autonomous-buddy-mvp.md) · [VI](docs/vi/autonomous-buddy-mvp_vi.md)

---

## Platforms

| Platform | Status | Folder |
|----------|--------|--------|
| **macOS 13+** | Native companion | [`macos/`](macos/) |
| Windows | Planned v1.2 (likely Tauri/Rust) | — |
| Linux (X11) | Planned v1.3 | — |

The MVP targets macOS only. Each platform lives in its own subfolder so toolchains don't leak between them. Cross-platform glue (protocol schemas, command formats) is captured in [`docs/autonomous-buddy.md`](docs/autonomous-buddy.md) so future ports stay aligned.

---

## macOS — quick start

Requirements: macOS 13 (Ventura)+, Swift 5.9+ (Xcode 15 or Command Line Tools).

The `Makefile` at this directory wraps everything. From `autonomous-buddy/`:

```bash
make help       # list all targets
make run        # dev — runs in foreground (Ctrl-C to stop)
make app        # build dist/AutonomousBuddy.app
make open       # launch the bundled .app
make install    # copy bundled .app to /Applications
make audit      # tail the audit log
make kill       # stop any running AutonomousBuddy
make clean      # remove all build artifacts
make mock       # run mock-device (Go) — test buddy without a real device
```

Behind the scenes `make run` calls `swift run` inside `macos/`. Use it if you don't want to remember the SPM commands.

### First launch (Gatekeeper)

No code signing yet. First time launching the bundled `.app` Gatekeeper will block it:

1. `make app` to build `dist/AutonomousBuddy.app`
2. Finder → right-click the `.app` → **Open** → confirm in the dialog
3. Subsequent launches work normally (`make open` or double-click)

Apple Developer signing + notarization comes in v2.0.

## Testing end-to-end with mock-device

Until the real device Go side lands, you can drive the buddy from a tiny Go mock server. See [`mock-device/README.md`](mock-device/README.md). Two terminals:

```bash
# Terminal 1
make run        # buddy menu bar app

# Terminal 2
make mock       # prints pairing code, drops you into a REPL
```

Then in buddy's menu → **Pair with device…** → host `localhost:8765` + the 6-digit code. The mock's REPL sends commands (`ping`, `open_app Calculator`, `type_text hello`, etc.) over the WebSocket.

---

## Folder layout

```
autonomous-buddy/
├── README.md           # this file
├── .gitignore
├── docs/               # design + MVP plan (EN + VI)
│   ├── autonomous-buddy.md
│   ├── autonomous-buddy-mvp.md
│   └── vi/
└── macos/              # macOS native (Swift) — current MVP target
    ├── Package.swift
    └── Sources/AutonomousBuddy/
        ├── main.swift
        ├── AppDelegate.swift
        ├── MenuBarController.swift
        ├── Discovery/   # Phase 1B — mDNS device discovery
        ├── Pairing/     # Phase 1C — 6-digit pairing + Keychain
        ├── Connection/  # Phase 1D — WebSocket to device
        ├── Commands/    # Phase 1E — command dispatcher + executors
        ├── Permissions/ # macOS permission helpers
        └── Audit/       # local audit log
```

---

## Computer-use implementation

- Menu bar, pairing, persistent device WebSocket, activity history, and Pause.
- App/URL actions, keyboard/mouse input, clipboard, screenshots, and display geometry.
- Bounded Accessibility trees and snapshot-scoped element actions for native apps.
- Device-side synchronous workflow helper, actual image loading, and auxiliary vision fallback for text-only agents.
- Cooperative cancellation, concurrent-input rejection, and connection-scoped responses.

Unit and transport tests cover these components. Live multi-app workflows, current
macOS permission behavior, and cancellation during real input still require the
[acceptance checklist](docs/computer-use.md#manual-acceptance-checklist). A command
returning success does not prove the user's whole task is complete.

---

## Comments policy

English only — see project `CLAUDE.md`.

## License

Same as the parent repository.
