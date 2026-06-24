# AGENTS.md

This file provides guidance to Codex and other coding agents when working in
this repository. Treat `CLAUDE.md` as the upstream source of truth; this file is
the Codex-compatible mirror of those project rules.

## Multi-IDE Rules

This repo is developed across multiple AI-assisted environments. The following
rules apply to all code changes:

1. **Update docs on code change** - When changing behavior, architecture, or
   APIs, update both the English and Vietnamese docs. Keep numbers, flows,
   endpoints, and states accurate with the code. Platform docs are in `docs/`;
   lamp-specific docs are in `devices/lamp/docs/`.

   **Platform docs** (`docs/` + `docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | os-server, API, startup | `docs/os-server.md` | `docs/vi/os-server_vi.md` |
   | Setup flow, provisioning | `docs/setup-flow.md` | `docs/vi/setup-flow_vi.md` |
   | Web UI, configuration pages | `docs/web-ui.md` | `docs/vi/web-ui_vi.md` |
   | Flow Monitor (turn pipeline, JSONL, SSE) | `docs/flow-monitor.md` | `docs/vi/flow-monitor_vi.md` |
   | Overall structure | `docs/overview.md` | `docs/vi/overview_vi.md` |
   | MQTT, dispatch, publish | `docs/mqtt.md` | `docs/vi/mqtt_vi.md` |
   | OTA, bootstrap | `docs/bootstrap-ota.md` | `docs/vi/bootstrap-ota.md` |
   | Speech emotion recognition (SER) | `docs/speech-emotion.md` | `docs/vi/speech-emotion_vi.md` |
   | Realtime voice agent (HAL `drivers/realtime`, Gemini Live / OpenAI Realtime, delegate) | `docs/realtime-voice.md` | `docs/vi/realtime-voice_vi.md` |
   | DL backend, load balancer, encryption, models | `docs/dlbackend.md` | `docs/vi/dlbackend_vi.md` |
   | Hermes agent backend (`agent_runtime`, internal/hermes) | `docs/agentic/hermes.md` | `docs/vi/agentic/hermes_vi.md` |
   | PicoClaw agent backend (`agent_runtime`, internal/picoclaw, WebSocket) | `docs/agentic/picoclaw.md` | `docs/vi/agentic/picoclaw_vi.md` |
   | Adding/changing an agentic backend (AgentGateway contract, switch, install/presync, migration, skills, hooks, reset) | `docs/agentic/adding-agent-runtime.md` | `docs/vi/agentic/adding-agent-runtime_vi.md` |
   | Safety engine (SAFETY.md bounds, deterministic enforcement gate) | `docs/safety.md` | `docs/vi/safety_vi.md` |

   **Lamp-specific docs** (`devices/lamp/docs/` + `devices/lamp/docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | LED, effects, states, animations | `devices/lamp/docs/led-control.md` | `devices/lamp/docs/vi/led-control_vi.md` |
   | Sensing behavior, sound escalation, reactions | `devices/lamp/docs/sensing-behavior.md` | `devices/lamp/docs/vi/sensing-behavior_vi.md` |
   | Sensing threshold tuning | `devices/lamp/docs/sensing-tuning.md` | `devices/lamp/docs/vi/sensing-tuning_vi.md` |
   | Habit tracking, pattern building, habit-aware nudge phrasing | `devices/lamp/docs/habit-tracking.md` | `devices/lamp/docs/vi/habit-tracking_vi.md` |
   | Vision tracking, object follow, servo track | `devices/lamp/docs/vision-tracking.md` | `devices/lamp/docs/vi/vision-tracking_vi.md` |
   | Physical controls (GPIO button, TTP223 touchpad, gestures, pet response) | `devices/lamp/docs/physical-controls.md` | `devices/lamp/docs/vi/physical-controls_vi.md` |
   | Autonomous Buddy (Mac companion app) | `autonomous-buddy/docs/autonomous-buddy.md`, `autonomous-buddy/docs/autonomous-buddy-mvp.md`, `autonomous-buddy/docs/release-signing.md` | `autonomous-buddy/docs/vi/autonomous-buddy_vi.md`, `autonomous-buddy/docs/vi/autonomous-buddy-mvp_vi.md`, `autonomous-buddy/docs/vi/release-signing_vi.md` |
   | Security test checklist | `devices/lamp/docs/security-test.md` | _(no vi version)_ |

2. **Comments in English** - Project standard.
3. **Code is the single source of truth** - Docs reflect code, not the other
   way around.
4. **Do not commit binary artifacts** - Version is injected via ldflags at
   build time.

See `docs/DEV-MULTI-IDE.md` for full conventions.

## Working Style

- The user reviews and commits by hand. Do not create commits unless explicitly
  asked.
- Work in small, reviewable chunks. When a task spans multiple concerns, split
  it by concern and verify each batch before moving to the next.
- Stay in scope. Flag unrelated issues instead of fixing them opportunistically.
- Verify with concrete evidence such as focused tests, builds, greps, `bash -n`,
  or compile checks. Report what was and was not verified.
- Do not "clean up" inherited drift such as unrelated gofmt churn, duplicate
  dependency metadata, or upstream-preserved style unless it is required for the
  task.
- Respond to the user in Vietnamese unless they request otherwise.
- Do not auto-deploy to devices. Default to repo changes plus local verification;
  any on-device SSH/SCP/restart step is opt-in and must be confirmed first.

## Parallel Work / Subagents

When work can be split across independent, file-scoped tasks, use available
parallelism instead of doing everything sequentially. In Codex, prefer
`multi_tool_use.parallel` for independent local reads/checks, and use subagents
only when the tool is available and the overhead is justified.

Common cases in this repo:

- Repetitive edits across many files, such as rebranding strings across EN + VI
  docs: split by file or language, with exact rules and a verification grep.
- Long-running builds or cross-compile checks, such as `swift build` or
  `GOOS=linux GOARCH=arm64 go build`: run in parallel/background when possible
  and continue with independent work.
- Repo-wide audits, such as stale paths after folder moves or broken cross-refs:
  use audit-only scope unless edits are explicitly part of the task.
- Independent English and Vietnamese doc updates after a code change: keep both
  sides consistent and verify matching numbers, endpoints, states, and flows.

Rules:

- Brief any delegated worker with goal, context, exact files/scope, verification
  step, and concise report format.
- Do not delegate when the overhead is larger than the work itself, especially
  for one or two quick edits in files already open.
- Trust but verify: spot-check actual diffs and run focused greps/tests before
  considering delegated work done.

## Device Access Rules

- Always ask the user before running any `sshpass` or `ssh` command to the Pi.
  Do not SSH automatically.
- Pi SSH: `ssh pi@<IP>` (credentials stored in the team password manager; IP
  varies per session).

## Project Overview

Autonomous is an open-source OS for physical AI agents. The Go backend
(`os/services`) provides device onboarding (WiFi, LLM provider, messaging
channel setup), OTA updates, and agent gateway integration. The brain is a
swappable agentic runtime (OpenClaw, Hermes, or any LLM + skills + memory).

**Go module (`os/services`):** `go.autonomous.ai/os` | **Go 1.24** | **Target:** Linux ARM64

## Build & Development Commands

All targets run from the repo root via the top-level `Makefile`.

```bash
# Build Go services (cross-compiles to linux/arm64)
make os-build                # Builds os-server binary
make os-build-bootstrap      # Builds bootstrap-server binary

# Code generation (Google Wire DI)
make os-generate             # Runs: cd os/services && GOFLAGS=-mod=mod go generate ./...

# Lint + tests (Go)
make os-lint                 # cd os/services && golangci-lint run
make os-test                 # cd os/services && go test ./...

# HAL (Python hardware runtime, os/hal)
make hal-dev                 # Install deps + run HAL locally
make hal-lint                # Catch broken local imports + undefined names
make hal-test                # Run HAL tests

# Web frontend (React/Vite/Tailwind in os/services/web)
make web-install             # npm install
make web-dev                 # Vite dev server
make web-build               # Production build to dist/
```

Go version is injected at build time via ldflags. HAL/web versions live in
`os/services/VERSION_OS_SERVER` and `os/hal/VERSION_HAL` and are auto-bumped by the
`make upload-*` release targets — do not hand-edit for releases.

## Architecture

### Two Executables

- `os/services/cmd/os-server/main.go` - Main HTTP API server (Gin). Handles device
  setup, network management, LED control, health checks, and agent gateway
  integration.
- `os/services/cmd/bootstrap/main.go` - OTA bootstrap worker. Periodically
  checks for and applies updates.

### Dependency Injection

Uses Google Wire for compile-time DI. After changing provider signatures, run
`make os-generate` to regenerate `wire_gen.go` files.

### Package Layout

**Go backend - `os/services/`:**

- `server/` - HTTP layer: Gin router, route handlers organized by domain.
  Each handler follows the `delivery/http/handler.go` convention.
- `internal/` - Business logic services (agent, ambient, beclient, buddy,
  device, healthwatch, intent, monitor, network, openclaw, ota, statusled).
- `bootstrap/` - OTA worker: metadata fetching, update execution, state
  persistence.
- `domain/` - Shared data structures.
- `server/serializers/` - Standard JSON response wrapper.
- `server/config/` - Config management.
- `lib/` - Shared libraries (mqtt, core/system, i18n, logger, hal HAL
  client, safego, ...).
- `web/` - React 19 + TypeScript + Vite + Tailwind CSS 4 SPA.

**HAL - `os/hal/` (Python hardware runtime, FastAPI on :5001):**

- `drivers/` - Hardware drivers by subsystem (rgb, motors, voice, sensing,
  display, gpio_button, ...).
- `board/` - Per-board profiles (pin maps, debounce).
- `routes/` - FastAPI route modules (servo, led, camera, audio, emotion, ...).

**OS-level dirs (repo root):** `contract/` (device specs), `skills/` (agent
skills), `devices/` (per-device declarations + docs), `cts/` (compliance
tests), `imager/` (OrangePi image build), `scripts/` (setup + OTA upload),
`dlbackend/`, `companions/`.

### API Response Format

All HTTP endpoints return:

```json
{"status": 1, "data": {}, "message": null}
```

on success, and:

```json
{"status": 0, "data": null, "message": "error"}
```

on failure.

### Configuration

Config lives in `config/config.json` (path relative to the os-server working
dir) and is managed by `os/services/server/config/config.go`. It supports a
notification channel for config change propagation.

## Coding Standards

### Error Handling

```go
if err != nil {
    return fmt.Errorf("operation: %w", err)
}
```

Always wrap errors with useful context.

### Logging

```go
log.Println("[component] message")
log.Printf("[component] formatted %v", value)
```

### Goroutines

Always use `context.Context` for cancellation. Background goroutines must
respect `ctx.Done()`.

### Validation

Use `go-playground/validator` for struct validation. Validate at the HTTP
handler level before passing data to services.

### Naming (paths under `os/services/`)

- Handlers: `server/<domain>/delivery/http/handler.go`
- Services: `internal/<domain>/service.go`
- Wire providers: `server/wire.go`, `bootstrap/wire.go`
- Domain types: `domain/<type>.go`
