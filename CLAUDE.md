# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Multi-IDE Rules (Cursor + Claude Code)

This repo is developed in both **Cursor** and **Claude Code**. The following rules (from `.cursor/rules/`) apply to all code changes:

1. **Update docs on code change** — When you change code that affects behavior, architecture, or APIs, update **both** the English and Vietnamese docs to match. Keep numbers, flows, endpoints, and states 100% accurate with the code. Platform docs are in `docs/`; lamp-specific docs are in `devices/lamp/docs/`.

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
   | Hermes agent backend (`agent_runtime`, internal/hermes) | `docs/hermes.md` | `docs/vi/hermes_vi.md` |
   | PicoClaw agent backend (`agent_runtime`, internal/picoclaw, WebSocket) | `docs/picoclaw.md` | `docs/vi/picoclaw_vi.md` |
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

2. **Comments in English** — Project standard.
3. **Code is the single source of truth** — Docs reflect code, not the other way around.
4. **Do not commit binary artifacts** — Version is injected via ldflags at build time.

See `docs/DEV-MULTI-IDE.md` for full conventions.

## Subagent Usage

When work can be split across independent, file-scoped tasks, spawn subagents in parallel instead of doing them sequentially. Common cases in this repo:

- **Repetitive edits across many files** (e.g. rebrand string across docs EN+VI): one subagent per file or per language, brief each with exact rules + verification grep
- **Long-running builds / cross-compile checks** (`swift build`, `GOOS=linux GOARCH=arm64 go build`): spawn in background, continue other work, react on notification
- **Repo-wide audits** (find stale paths after folder rename, find broken cross-refs): spawn an `Explore` subagent with audit-only scope (no edits), let it report back
- **Independent doc updates** (English + Vietnamese counterparts after a code change): spawn two agents in parallel

Rules:
- Spawn multiple agents in a **single message with multiple tool calls** for parallelism. Sequential `Agent` calls don't parallelize.
- Use `run_in_background: true` for builds/long tasks; foreground for "I need the result to continue".
- Brief each agent like a smart colleague: goal + context + already-done + exact rules + verification step + report format/length cap.
- Don't delegate when overhead > the work itself (e.g. 1–2 quick edits in files you've already read).
- Trust but verify: each agent reports what it intended to do; spot-check the actual diff before marking task done.

## Device Access Rules

- **Always ask the user before running any `sshpass` or `ssh` command to the Pi.** Do not SSH automatically.
- Pi SSH: `ssh pi@<IP>` (credentials stored in team password manager; IP varies per session).

## Project Overview

Autonomous is an open-source OS for physical AI agents. The Go backend (`os/services`) provides device onboarding (WiFi, LLM provider, messaging channel setup), OTA updates, and agent gateway integration. The brain is a swappable agentic runtime (OpenClaw, Hermes, or any LLM + skills + memory).

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
make hal-dev                 # Install deps + run hal locally
make hal-lint                # Catch broken local imports + undefined names (refactor leftovers)
make hal-test                # Run HAL tests

# Web frontend (React/Vite/Tailwind in os/services/web)
make web-install             # npm install
make web-dev                 # Vite dev server
make web-build               # Production build → dist/
```

Go version is injected at build time via ldflags. HAL/web versions live in
`os/services/VERSION_OS_SERVER` and `os/hal/VERSION_HAL` and are auto-bumped by the
`make upload-*` release targets — do not hand-edit for releases.

## Architecture

### Two Executables

- **`os/services/cmd/os-server/main.go`** — Main HTTP API server (Gin). Handles device setup, network management, LED control, health checks, and agent gateway integration.
- **`os/services/cmd/bootstrap/main.go`** — OTA bootstrap worker. Periodically checks for and applies updates.

### Dependency Injection

Uses **Google Wire** for compile-time DI. After changing provider signatures, run `make os-generate` to regenerate `wire_gen.go` files.

### Package Layout

**Go backend — `os/services/`:**

- **`server/`** — HTTP layer: Gin router, route handlers organized by domain. Each handler follows `delivery/http/handler.go` convention.
- **`internal/`** — Business logic services (agent, ambient, beclient, buddy, device, healthwatch, intent, monitor, network, openclaw, ota, statusled).
- **`bootstrap/`** — OTA worker: metadata fetching, update execution, state persistence.
- **`domain/`** — Shared data structures.
- **`server/serializers/`** — Standard JSON response wrapper.
- **`server/config/`** — Config management.
- **`lib/`** — Shared libraries (mqtt, core/system, i18n, logger, hal HAL client, safego, …).
- **`web/`** — React 19 + TypeScript + Vite + Tailwind CSS 4 SPA.

**HAL — `os/hal/` (Python hardware runtime, FastAPI on :5001):**

- **`drivers/`** — Hardware drivers by subsystem (rgb, motors, voice, sensing, display, gpio_button, …).
- **`board/`** — Per-board profiles (pin maps, debounce).
- **`routes/`** — FastAPI route modules (servo, led, camera, audio, emotion, …).

**OS-level dirs (repo root):** `contract/` (device specs), `skills/` (agent skills), `devices/` (per-device declarations + docs), `cts/` (compliance tests), `imager/` (OrangePi image build), `scripts/` (setup + OTA upload), `dlbackend/`, `companions/`.

### API Response Format

All HTTP endpoints return: `{"status": 1, "data": <payload>, "message": null}` on success, `{"status": 0, "data": null, "message": "error"}` on failure.

### Configuration

Config lives in `config/config.json` (path relative to the os-server working dir). Managed by `os/services/server/config/config.go`. Supports notification channel for config change propagation.

## Coding Standards

### Error Handling
```go
if err != nil {
    return fmt.Errorf("operation: %w", err)  // Always wrap with context
}
```

### Logging
```go
log.Println("[component] message")
log.Printf("[component] formatted %v", var)
```

### Goroutines
Always use `context.Context` for cancellation. Background goroutines must respect `ctx.Done()`.

### Validation
Use `go-playground/validator` for struct validation. Validate at HTTP handler level before passing to services.

### Naming (paths under `os/services/`)
- Handlers: `server/<domain>/delivery/http/handler.go`
- Services: `internal/<domain>/service.go`
- Wire providers: `server/wire.go`, `bootstrap/wire.go`
- Domain types: `domain/<type>.go`
