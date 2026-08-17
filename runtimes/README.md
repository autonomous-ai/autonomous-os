# Agent runtimes

The swappable **brains** of Autonomous OS. Each folder is one complete backend that drives a
device through the same `AgentGateway` contract (`system/domain`): it receives sensing events
and chat as turns, runs the device's skills and `SOUL.md`, and replies through TTS / web chat /
channels. Everything below the brain (System Managers, HAL, safety) runs the same regardless
of which runtime is active — that is the point.

| Runtime | What it is | Wire transport | Docs |
|---------|-----------|----------------|------|
| [`openclaw/`](openclaw/) | OpenClaw daemon (default brain) | persistent WebSocket (`127.0.0.1:18789`) | [`docs/os-server.md`](../docs/os-server.md) |
| [`hermes/`](hermes/) | Hermes API server | HTTP + SSE (`127.0.0.1:8642`) | [`docs/agentic/hermes.md`](../docs/agentic/hermes.md) |
| [`picoclaw/`](picoclaw/) | PicoClaw runtime | persistent WebSocket | [`docs/agentic/picoclaw.md`](../docs/agentic/picoclaw.md) |
| [`codex/`](codex/) | OpenAI Codex CLI behind a local WS bridge | WebSocket (bridge: `os-server codex-gatewayd`) | [`docs/agentic/codex.md`](../docs/agentic/codex.md) |
| [`claudecode/`](claudecode/) | Claude Code CLI behind a local WS bridge | WebSocket (bridge: `os-server claudecode-gatewayd`) | [`docs/agentic/claudecode.md`](../docs/agentic/claudecode.md) |
| [`opencode/`](opencode/) | opencode CLI behind a local WS bridge | WebSocket (bridge: `os-server opencode-gatewayd`) | [`docs/agentic/opencode.md`](../docs/agentic/opencode.md) |

## How a runtime is selected

`system/agent/factory.go` (`ProvideGateway`) picks the backend at boot:
`config.agent_runtime` → the device's declared `gateway.default` in
`devices/<type>/ROBOT.md` → OpenClaw. Switching at runtime is a first-class flow — web
Settings, MQTT `agent_runtime.set`, or `POST /api/device/agent-runtime` — and `system/agent/`
also owns everything that must migrate on a switch: persona (`migrate_persona`, canonical
`PersonaBundle` with one adapter per runtime), LLM config, channels, and MCP connectors, each
reconciled with an "applied-runtime" marker so a clean switch is a no-op.

## What every runtime folder contains

The six backends deliberately mirror each other — learn one, read any:

- **Gateway client** (`service.go`, `client.go`, `chat.go`) — connection loop, `sendChat`
  (marks busy + fires the `thinking` face), run/session tracking, `ShouldRotateSession`.
- **`onboarding.go`** — seeds the runtime's workspace (persona from `SOUL.md`, skills,
  system prompt) and self-heals its config from `config.json` on every boot.
- **`install.sh` + `presync.sh`** — embedded into the os-server binary and registered via
  `system/lib/runtimereg` (an `init()` in each runtime), so switching to a backend can
  install it on-device with zero imager changes.
- **`skill_watcher.go`** — installs/updates the device's `SKILL.md` set (capability-filtered
  by `system/skills`) into the runtime's native skills location.
- **Channels** (`telegram*.go`, `discord.go`, `slack*.go`) — device-owned chat channels,
  mirrored 1:1 across codex/claudecode/opencode/hermes/picoclaw.
- **`emotion_ack.go`** — native mirror of OpenClaw's `emotion-acknowledge` hook (same skip
  rules, intensity, capability gate). OpenClaw itself runs the TS original in
  [`openclaw/hooks/`](openclaw/hooks/). ⚠️ Keep all six in lockstep.
- **`identity.go`** — watches `SOUL.md` for wake words / identity changes.
- **`reset.go`** — the backend's share of factory reset (workspace wipe).
- **`PROGRESS.md` / `resources/KNOWLEDGE.md`** — per-runtime state of the port and the
  knowledge file seeded into its workspace.

`codex/`, `claudecode/` and `opencode/` additionally ship a `gatewayd/` — a WS bridge
compiled into os-server and run as a systemd unit via the `os-server codex-gatewayd` /
`os-server claudecode-gatewayd` / `os-server opencode-gatewayd` subcommands, so the
CLI-based brains speak the same persistent-socket protocol as the rest.

## Adding your own

A new brain is one folder here + one factory case. The full contract (AgentGateway methods,
install/presync, migration adapters, skills, hooks, reset) is documented in
[`docs/agentic/adding-agent-runtime.md`](../docs/agentic/adding-agent-runtime.md).
