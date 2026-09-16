# Architecture

Autonomous is a layered stack. Each layer exposes an interface to the layer above and
depends only on the one below, so any layer can be replaced without touching the others.

![Autonomous OS stack, top down: 25 skills, six swappable agent runtimes, 14 Go system packages, the realtime voice agent, 13 HAL capabilities, a deterministic safety gate below them (brightness, quiet hours, explicit-move speed, thermal today), in-tree drivers and board profiles, the vendor Linux kernel, and the bodies — each row labelled with its repo folder.](autonomous-stack.png)

## Layers

**Skills** — what the device does: 25 skills, each a `SKILL.md` the runtime invokes — apps
like `guard`, `mood`, `scene`, `habit`, `wellbeing`, `skill-creator`, plus capability wrappers
(`led-control`, `servo-control`, `camera`, `music`, …). A skill is an *ability*; the device's
*character* is its `SOUL.md`. First-party skills use the same public contract a third party
gets. *(`skills/`)*

**System Managers** — the always-on Go daemon: `intent` (fast local commands), `network`,
`sensing` routing, `monitor` (flow event bus), `healthwatch`, `ambient`, and `device`.
Deterministic — they run with or without the runtime. OTA runs as its own worker
(`bootstrap/`). *(`system/`)*

**Agentic Runtime** — **OpenClaw**, **Hermes**, **PicoClaw**, **OpenAI Codex**, **Claude Code**,
or a custom runtime. Runs the skills, embodies the device's `SOUL.md`, and decides what to act
on. Swappable — and where Autonomous's differentiated value (the default brain, memory,
character) lives. Its **tools** — how it reaches beyond the device — are **MCP connectors**
(`runtimes/*/mcp.go`, synced across a switch by `system/agent`) and the **CLI** the LLM calls
directly; skills are the device's own abilities through the HAL, tools are external.
*(`runtimes/{openclaw,hermes,picoclaw,codex,claudecode,opencode}`)*

**HAL — Capabilities** — the frozen, versioned interface, 13 capabilities: `audio`, `vision`,
`sensing`, `presence`, `motion`, `light`, `display`, `expression`, `lifelike`, `media`, `connectivity`,
`companion`, `system`. Skills call capabilities (`motion.move`), never hardware models, so one
skill runs on any supported body that implements the capability — for example, Lamp's servo
arm serves `motion`; Unitree Go2-W remains a declaration-only reference until its port exists.
A device's `DEVICE.md` declares which it has; the runtime mounts only those. The HAL also hosts
the **safety gate** (`hal/safety`): `SAFETY.md` bounds —
e-stop, motion limits, brightness, quiet hours — enforced deterministically below the brain,
never by the LLM.
*(`robots/contract/` + `hal` — see [hal.md](hal.md))*

**Agentic Middle** — the realtime voice agent (`hal/realtime`, hosted in-process by the HAL but
brain-tier, so it is drawn as its own band). Voice turns land here first, and it decides per turn:
**answer directly** when the turn is simple (small talk, no skills or tools), or **delegate up** to
the main agentic runtime (the `delegate_to_main` tool call) when the turn needs skills or complex tool calls. Runs on
Gemini Live or OpenAI Realtime — see [realtime-voice.md](../realtime-voice.md).

**Linux Kernel** — the vendor kernel (Raspberry Pi OS / OrangePi, or a robot's onboard compute)
we run on; we don't ship one. Our **Drivers** (`motors`, `rgb`, `display`, `camera`, `voice`
(STT/TTS/VAD), `gpio`/`touch`, `bluetooth` — `hal/drivers`, with per-board wiring in
`hal/board`) are userspace programs talking to it through GPIO/SPI/ALSA/V4L2; **Power Management** is the foundation. *(see [kernel.md](kernel.md))*

## See also

[hal.md](hal.md) · [kernel.md](kernel.md) ·
[`ROBOT-SPEC.md`](../../robots/contract/ROBOT-SPEC.md) ·
[`capabilities.md`](../../robots/contract/capabilities.md)
