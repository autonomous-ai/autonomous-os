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

```bash
cd macos
swift --version    # verify
swift run          # dev — shows 💡 in menu bar
```

A 💡 icon appears in the macOS menu bar. Click it for the menu (Pair, About, Quit). No Dock icon (intentional — accessory-only app).

Release build:

```bash
cd macos
swift build -c release
./.build/release/LumiBuddy
```

### Distribution (MVP)

No code signing or notarization yet. First run is blocked by Gatekeeper:

1. Build the release binary as above
2. Bundle into an `.app` (TODO — Phase 1I)
3. Right-click the `.app` → **Open** → confirm in the dialog

Apple Developer signing comes in v2.0.

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
