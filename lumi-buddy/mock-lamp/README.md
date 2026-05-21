# mock-lamp

Tiny Go server that mocks the Lumi lamp's buddy contract. Use it to test the macOS `lumi-buddy` app end-to-end **without** running the real lumi Go server.

It doubles as a **reference implementation** for the eventual lumi-side work: the file structure, types, and dispatch loop here mirror what `lumi/internal/buddy/` and `lumi/server/buddy/delivery/http/` will end up looking like.

## Run

From `lumi-buddy/`:

```bash
make mock
```

(or `cd mock-lamp && go run .` directly)

The server listens on `127.0.0.1:8765` and prints a 6-digit code:

```
[mock-lamp] listening on http://127.0.0.1:8765
[hint] In Lumi Buddy: menu → 'Pair with Lumi…' → host: localhost:8765 + code below

┌─────────────────────────────────────────────┐
│  Pairing code:  123456                      │
│  Host in buddy: localhost:8765              │
└─────────────────────────────────────────────┘
```

## Pair the buddy against mock

In another terminal:

```bash
make run
```

Click the 💡 in the menu bar → **Pair with Lumi…** → type:
- Host: `localhost:8765`
- Code: the 6-digit number from mock terminal

Buddy stores the token in Keychain and opens a WebSocket. Mock prints `✓ buddy connected: buddy-xxxx` and the buddy's menu bar shows the green ✅.

## Send commands (REPL)

In the mock terminal, type a command and press enter:

```
> ping
  ✓ map[pong:true timestamp:2026-05-21T...]  (8ms)

> notification meeting in 5
  ✓ map[delivered:true]  (12ms)

> open_url https://news.ycombinator.com
  ✓ map[browser:default opened:true]  (143ms)

> open_app Calculator
  ✓ map[bundle_id:com.apple.calculator pid:12345]  (320ms)

> type_text hello from lumi
  ✓ map[typed_chars:15]  (245ms)
  (needs Accessibility permission — grant on first try, then re-run)

> key_combo cmd space
  ✓ map[dispatched:true]  (3ms)
  (opens Spotlight)
```

| command | calls | macOS permission |
|---------|-------|------------------|
| `ping` | PingExecutor | none |
| `open_app <name>` | NSWorkspace + bundle ID lookup | none |
| `close_app <name>` | AppleScript `tell app to quit` | Automation (per-app, first time) |
| `open_url <url>` | NSWorkspace.open | none |
| `type_text <text>` | CGEvent + UnicodeString | Accessibility |
| `key_combo <keys>` | CGEvent with modifier flags | Accessibility |
| `notification <title>` | UNUserNotificationCenter | Notifications (first time) |
| `code` | re-issue pairing code | — |
| `status` | show pairing/ws state | — |
| `help` / `quit` | | — |

## File layout (= reference for production lumi side)

```
mock-lamp/
├── main.go      # http.Server bootstrap, signal handling, REPL goroutine
├── state.go     # State + PairingRecord + pending-by-ID registry
├── pairing.go   # /api/buddy/pair/{start,confirm} handlers
├── ws.go        # /api/buddy/ws upgrade + reader loop + Dispatch()
├── command.go   # Command shape (matches buddy's IncomingCommand) + REPL parser
└── repl.go      # interactive command sender
```

When the real lumi-side work happens, expect roughly:

- `lumi/server/buddy/delivery/http/handler_pair.go` ← `pairing.go`
- `lumi/server/buddy/delivery/http/handler_ws.go` ← `ws.go` (HandleWS only)
- `lumi/internal/buddy/dispatcher.go` ← `ws.go` (Dispatch + pending)
- `lumi/internal/buddy/types.go` ← `command.go` (Command struct)
- `lumi/internal/buddy/store.go` ← `state.go` (PairingRecord persistence — `buddies.json` instead of in-memory)
- `lumi/internal/buddy/pairing.go` ← `state.go` (code generation + token issuance, with admin auth on /start)

## What the mock does NOT do (vs production)

- **No persistence** — restart wipes pairings (production: `config/buddies.json`)
- **No admin auth on /pair/start** — production gates this behind admin cookie
- **Single buddy** — MVP design is 1↔1, but production may want a list
- **No TLS** — `ws://` only, LAN dev tool
- **No rate limiting** — production should cap commands/sec/buddy

Throw it away (or keep as a fixture for `go test`) once `lumi/internal/buddy/` lands.
