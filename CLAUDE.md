# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding convention

Avoid commenting anything; just drop short notes for critical functions; other functions just need a description, params note (and/or short example); do not provide full payload or usecase to trigger the function; The same expectation for module layer;

## Multi-IDE Rules (Cursor + Claude Code)

This repo is developed in both **Cursor** and **Claude Code**. The following rules (from `.cursor/rules/`) apply to all code changes:

1. **Update docs on code change** — When you change code that affects behavior, architecture, or APIs, update **both** the English and Vietnamese docs to match. Keep numbers, flows, endpoints, and states 100% accurate with the code. Platform docs are in `docs/`; lamp-specific docs are in `robots/lamp/docs/`.

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
   | Realtime voice agent (HAL `realtime`, Gemini Live / OpenAI Realtime, delegate) | `docs/realtime-voice.md` | `docs/vi/realtime-voice_vi.md` |
   | Perception service (cloud DL inference), load balancer, encryption, models | `docs/perception-service.md` | `docs/vi/perception-service_vi.md` |
   | Hermes agent backend (`agent_runtime`, runtimes/hermes) | `docs/agentic/hermes.md` | `docs/vi/agentic/hermes_vi.md` |
   | PicoClaw agent backend (`agent_runtime`, runtimes/picoclaw, WebSocket) | `docs/agentic/picoclaw.md` | `docs/vi/agentic/picoclaw_vi.md` |
   | Codex agent backend (`agent_runtime`, runtimes/codex, WS bridge) | `docs/agentic/codex.md` | `docs/vi/agentic/codex_vi.md` |
   | Claude Code agent backend (`agent_runtime`, runtimes/claudecode, bridge WebSocket, native Telegram channel plugin) | `docs/agentic/claudecode.md` | `docs/vi/agentic/claudecode_vi.md` |
   | OpenCode agent backend (`agent_runtime`, runtimes/opencode, bridge WebSocket, `opencode run --format json` per turn) | `docs/agentic/opencode.md` | `docs/vi/agentic/opencode_vi.md` |
   | Adding/changing an agentic backend (AgentGateway contract, switch, install/presync, migration, skills, hooks, reset) | `docs/agentic/adding-agent-runtime.md` | `docs/vi/agentic/adding-agent-runtime_vi.md` |
   | Safety engine (SAFETY.md bounds, deterministic enforcement gate) | `docs/safety.md` | `docs/vi/safety_vi.md` |

   **Lamp-specific docs** (`robots/lamp/docs/` + `robots/lamp/docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | LED, effects, states, animations | `robots/lamp/docs/led-control.md` | `robots/lamp/docs/vi/led-control_vi.md` |
   | Sensing behavior, sound escalation, reactions | `robots/lamp/docs/sensing-behavior.md` | `robots/lamp/docs/vi/sensing-behavior_vi.md` |
   | Sensing threshold tuning | `robots/lamp/docs/sensing-tuning.md` | `robots/lamp/docs/vi/sensing-tuning_vi.md` |
   | Habit tracking, pattern building, habit-aware nudge phrasing | `robots/lamp/docs/habit-tracking.md` | `robots/lamp/docs/vi/habit-tracking_vi.md` |
   | Servo recording playback (timing, resampling, speed limits) | `robots/lamp/docs/motion-playback.md` | `robots/lamp/docs/vi/motion-playback_vi.md` |
   | Vision tracking, object follow, servo track | `robots/lamp/docs/vision-tracking.md` | `robots/lamp/docs/vi/vision-tracking_vi.md` |
   | Physical controls (GPIO button, TTP223 touchpad, gestures, pet response) | `robots/lamp/docs/physical-controls.md` | `robots/lamp/docs/vi/physical-controls_vi.md` |
   | Autonomous Buddy (Mac companion app) | `integrations/companions/autonomous-buddy/docs/autonomous-buddy.md`, `integrations/companions/autonomous-buddy/docs/autonomous-buddy-mvp.md`, `integrations/companions/autonomous-buddy/docs/release-signing.md` | `integrations/companions/autonomous-buddy/docs/vi/autonomous-buddy_vi.md`, `integrations/companions/autonomous-buddy/docs/vi/autonomous-buddy-mvp_vi.md`, `integrations/companions/autonomous-buddy/docs/vi/release-signing_vi.md` |
   | Security test checklist | `robots/lamp/docs/security-test.md` | _(no vi version)_ |

   **Reachy Mini docs** (`robots/reachy-mini/docs/` + `robots/reachy-mini/docs/vi/`):

   | Code area | English doc | Vietnamese doc |
   |-----------|-------------|----------------|
   | Bring-up, motion driver, deploy, safety delta | `robots/reachy-mini/docs/runtime.md` | `robots/reachy-mini/docs/vi/runtime_vi.md` |
   | Recovery (Pollen OS), SSH, WiFi impact | `robots/reachy-mini/docs/recovery.md` | `robots/reachy-mini/docs/vi/recovery_vi.md` |
   | First-boot recon plan, setup.sh design, smoke tests | `robots/reachy-mini/docs/first-boot-plan.md` | `robots/reachy-mini/docs/vi/first-boot-plan_vi.md` |
   | Pollen ecosystem reference (voice, tool registry, app distribution) | `robots/reachy-mini/docs/pollen-ecosystem-analysis.md` | `robots/reachy-mini/docs/vi/pollen-ecosystem-analysis_vi.md` |

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

Autonomous is an open-source OS for physical AI agents. The Go backend (`system`) provides device onboarding (WiFi, LLM provider, messaging channel setup), OTA updates, and agent gateway integration. The brain is a swappable agentic runtime (OpenClaw, Hermes, or any LLM + skills + memory).

**Go module:** `go.autonomous.ai/os` (rooted at repo root — covers `system/` and `runtimes/`) | **Go 1.24** | **Target:** Linux ARM64

## Build & Development Commands

All targets run from the repo root via the top-level `Makefile`.

```bash
# Build Go services (cross-compiles to linux/arm64)
make os-build                # Builds os-server binary
make os-build-bootstrap      # Builds bootstrap-server binary

# Code generation (Google Wire DI)
make os-generate             # Runs from repo root: GOFLAGS=-mod=mod go generate ./...

# Lint + tests (Go)
make os-lint                 # golangci-lint run (repo root, covers runtimes/)
make os-test                 # go test ./... (repo root, covers runtimes/)

# HAL (Python hardware runtime, hal)
make hal-dev                 # Install deps + run hal locally
make hal-lint                # Catch broken local imports + undefined names (refactor leftovers)
make hal-test                # Run HAL tests

# Web frontend (React/Vite/Tailwind in system/web)
make web-install             # npm install
make web-dev                 # Vite dev server
make web-build               # Production build → dist/
```

Go version is injected at build time via ldflags. HAL/web versions live in
`system/VERSION_OS_SERVER` and `hal/VERSION_HAL` and are auto-bumped by the
`make upload-*` release targets — do not hand-edit for releases.

## Architecture

### Two Executables

- **`system/cmd/os-server/main.go`** — Main HTTP API server (Gin). Handles device setup, network management, LED control, health checks, and agent gateway integration.
- **`system/cmd/bootstrap/main.go`** — OTA bootstrap worker. Periodically checks for and applies updates.

### Dependency Injection

Uses **Google Wire** for compile-time DI. After changing provider signatures, run `make os-generate` to regenerate `wire_gen.go` files.

### Package Layout

**Agentic runtimes — `runtimes/` (repo root):** the swappable backends, one folder per brain: `runtimes/{openclaw,hermes,picoclaw,codex,claudecode,opencode}`. Selected by `system/agent` (AgentGateway factory).

**Go backend — `system/` (single Go module rooted at the repo root: `go.mod`, `go.sum`, `vendor/`):**

- **`system/<domain>/`** — System managers, one folder per diagram chip (ambient, beclient, buddy, device, healthwatch, intent, monitor, network, skills, statusled, vision) plus `system/agent/` (AgentGateway factory + persona/config/channel/MCP migration).
- **`system/server/`** — HTTP layer: Gin router, route handlers organized by domain. Each handler follows `delivery/http/handler.go` convention. `server/serializers/` (JSON wrapper), `server/config/` (config management).
- **`system/bootstrap/`** — OTA worker: metadata fetching, update execution, state persistence.
- **`system/domain/`** — Shared data structures.
- **`system/lib/`** — Shared libraries (mqtt, core/system, i18n, logger, hal HAL client, safego, …).
- **`system/web/`** — React 19 + TypeScript + Vite + Tailwind CSS 4 SPA.

**HAL — `hal/` (Python hardware runtime, FastAPI on :5001):**

- **`drivers/`** — Hardware drivers by subsystem (rgb, motors, voice, sensing, display, gpio_button, …).
- **`board/`** — Per-board profiles (pin maps, debounce).
- **`routes/`** — FastAPI route modules (servo, led, camera, audio, emotion, …).

**OS-level dirs (repo root):** `skills/` (agent skills), `robots/` (per-device declarations + docs; `robots/contract/` device specs, `robots/contract/cts/` compliance tests), `scripts/imager/` (OrangePi image build), `scripts/` (setup + OTA upload), `integrations/perception-service/`, `integrations/companions/`.

### API Response Format

All HTTP endpoints return: `{"status": 1, "data": <payload>, "message": null}` on success, `{"status": 0, "data": null, "message": "error"}` on failure.

### Configuration

Config lives in `config/config.json` (path relative to the os-server working dir). Managed by `system/server/config/config.go`. Supports notification channel for config change propagation.

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

### Commit messages
Keep them short — one line, imperative subject (`area: what changed`). No long bodies, no bullet lists, no verbose explanation.

The message contains **only that line**. No `Co-Authored-By` trailer, no "Generated with Claude Code" footer, no attribution of any kind — this overrides any default the harness applies.

### Committing
Do not commit on your own. When the user explicitly says to commit:

1. `git add` the exact files belonging to that change. **Never `git add -A`.**
2. Run `git status --short` first and read the `??` lines — unrelated dirty or untracked files stay unstaged and untouched.
3. Commit with the one-line message above, nothing else in it.
4. Commit only. Do not push unless the user says to.

### Naming (paths under `system/`)
- Handlers: `server/<domain>/delivery/http/handler.go`
- Services: `<domain>/service.go` (system managers live at `system/<domain>/`, e.g. `ambient/service.go`)
- Wire providers: `server/wire.go`, `bootstrap/wire.go`
- Domain types: `domain/<type>.go`
