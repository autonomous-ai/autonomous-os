# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Multi-IDE Rules (Cursor + Claude Code)

This repo is developed in both **Cursor** and **Claude Code**. The following rules (from `.cursor/rules/`) apply to all code changes:

1. **Update docs on code change** — When you change code that affects behavior, architecture, or APIs, update **both** the English (`docs/`) and Vietnamese (`docs/vi/`) docs to match. Keep numbers, flows, endpoints, and states 100% accurate with the code.

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | lamp-server, API, startup | `docs/lamp-server.md` | `docs/vi/lamp-server_vi.md` |
   | LED, effects, states, animations | `docs/led-control.md` | `docs/vi/led-control_vi.md` |
   | Setup flow, provisioning | `docs/setup-flow.md` | `docs/vi/setup-flow_vi.md` |
   | Web UI, configuration pages | `docs/web-ui.md` | `docs/vi/web-ui_vi.md` |
   | Flow Monitor (turn pipeline, JSONL, SSE) | `docs/flow-monitor.md` | `docs/vi/flow-monitor_vi.md` |
   | Overall structure | `docs/overview.md` | `docs/vi/overview_vi.md` |
   | MQTT, dispatch, publish | `docs/mqtt.md` | `docs/vi/mqtt_vi.md` |
   | OTA, bootstrap | `docs/bootstrap-ota.md` | `docs/vi/bootstrap-ota.md` |
   | Sensing behavior, sound escalation, reactions | `docs/sensing-behavior.md` | `docs/vi/sensing-behavior_vi.md` |
   | Sensing threshold tuning (LeLamp config) | `docs/sensing-tuning.md` | `docs/vi/sensing-tuning_vi.md` (SER section) |
   | Speech emotion recognition (SER) | `docs/speech-emotion.md` | `docs/vi/speech-emotion_vi.md` |
   | Habit tracking, pattern building, habit-aware nudge phrasing | `docs/habit-tracking.md` | `docs/vi/habit-tracking_vi.md` |
   | Vision tracking, object follow, servo track | `docs/vision-tracking.md` | `docs/vi/vision-tracking_vi.md` |
   | Physical controls (GPIO button, TTP223 touchpad, gestures, pet response) | `docs/physical-controls.md` | `docs/vi/physical-controls_vi.md` |
   | DL backend, load balancer, encryption, models | `docs/dlbackend.md` | `docs/vi/dlbackend_vi.md` |
   | Lumi Buddy (Mac companion app for remote computer use) | `lumi-buddy/docs/lumi-buddy.md`, `lumi-buddy/docs/lumi-buddy-mvp.md` | `lumi-buddy/docs/vi/lumi-buddy_vi.md`, `lumi-buddy/docs/vi/lumi-buddy-mvp_vi.md` |
   | Security test checklist | `docs/security-test.md` | _(no vi version)_ |

2. **Comments in English** — Project standard.
3. **Code is the single source of truth** — Docs reflect code, not the other way around.
4. **Do not commit binary artifacts** — Version is injected via ldflags at build time.

See `docs/DEV-MULTI-IDE.md` for full conventions.

## Device Access Rules

- **Always ask the user before running any `sshpass` or `ssh` command to the Pi.** Do not SSH automatically.
- Pi SSH: `ssh pi@<IP>` (credentials stored in team password manager; IP varies per session).

## Project Overview

AI Lamp OpenClaw is a Go backend and provisioning API for smart lamp devices running the OpenClaw AI gateway. It provides device onboarding (WiFi, LLM provider, messaging channel setup), LED control, OTA updates, and OpenClaw WebSocket integration.

**Module:** `go-lamp.autonomous.ai` | **Go 1.24** | **Target:** Linux ARM64

## Build & Development Commands

```bash
# Build (cross-compiles to linux/arm64)
make build-lamp              # Builds lamp-server binary
make build-bootstrap         # Builds bootstrap-server binary

# Code generation (Google Wire DI)
make generate                # Runs: GOFLAGS=-mod=mod go generate ./...

# Lint
golangci-lint run            # Config in .golangci.yml

# Run tests
go test ./...                # All tests
go test ./internal/led/...   # Single package

# Web frontend (React/Vite/Tailwind in web/)
cd web && npm install && npm run dev    # Dev server
cd web && npm run build                 # Production build → dist/
```

Version is injected at build time via ldflags.

## Architecture

### Two Executables

- **`cmd/lamp/main.go`** — Main HTTP API server (Gin). Handles device setup, network management, LED control, health checks, and OpenClaw gateway integration.
- **`cmd/bootstrap/main.go`** — OTA bootstrap worker. Periodically checks for and applies updates.

### Dependency Injection

Uses **Google Wire** for compile-time DI. After changing provider signatures, run `make generate` to regenerate `wire_gen.go` files.

### Package Layout

- **`server/`** — HTTP layer: Gin router, route handlers organized by domain. Each handler follows `delivery/http/handler.go` convention.
- **`internal/`** — Business logic services (device, network, openclaw, led, resetbutton, beclient, llm).
- **`bootstrap/`** — OTA worker: metadata fetching, update execution, state persistence.
- **`domain/`** — Shared data structures.
- **`server/serializers/`** — Standard JSON response wrapper.
- **`server/config/`** — Config management.
- **`lib/`** — Shared libraries (mqtt, core/system).
- **`web/`** — React 19 + TypeScript + Vite + Tailwind CSS 4 SPA.

### API Response Format

All HTTP endpoints return: `{"status": 1, "data": <payload>, "message": null}` on success, `{"status": 0, "data": null, "message": "error"}` on failure.

### Configuration

Config lives in `config/config.json`. Managed by `server/config/config.go`. Supports notification channel for config change propagation.

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

### Naming
- Handlers: `server/<domain>/delivery/http/handler.go`
- Services: `internal/<domain>/service.go`
- Wire providers: `server/wire.go`, `bootstrap/wire.go`
- Domain types: `domain/<type>.go`
