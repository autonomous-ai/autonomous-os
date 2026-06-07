# Architecture

Autonomous is a layered stack. Each layer exposes an interface to the layer above and
depends only on the one below, so any layer can be replaced without touching the others.
(The layering follows Android; the driver/board split follows Linux.)

![Autonomous architecture](autonomous-stack.svg)

## Layers

**Skills** — what the device does: `guard`, `mood`, `scene`, `habit`, `wellbeing`. Each is a
`SKILL.md` the runtime invokes. A skill is an *ability*; the device's *character* is its
`SOUL.md`. First-party skills use the same public contract a third party gets. *(`skills/`)*

**Tools** — how the runtime reaches beyond the device: **MCP** servers and the **CLI**. Skills
are the device's own abilities (through the HAL); tools are external capabilities the runtime
calls.

**System Managers** — the always-on Go daemon: `intent` (fast local commands), `network`,
`OTA`, `sensing` routing, `health`, and `safety`. Deterministic — they run with or without the
runtime, and safety-critical actions (e-stop, motion limits) are enforced here, never by the
LLM. *(`os/services`)*

**Agentic Runtime** — **OpenClaw**, **Hermes**, or a custom runtime. Runs the skills, embodies
the device's `SOUL.md`, and decides what to act on. Swappable — and where Autonomous's
differentiated value (the default brain, memory, character) lives. *(`os/services/internal/openclaw`)*

**HAL — Capabilities** — the frozen, versioned interface: `audio`, `vision`, `motion`, `light`,
`display`, `presence`. Skills call capabilities (`motion.move`), never hardware models, so one
skill runs on any body that declares the capability — Lamp's servo arm and the Unitree Go2-W's
wheels both serve `motion`. A device's `DEVICE.md` declares which it has; the runtime mounts only those.
*(`contract/` + `os/hal` — see [hal.md](hal.md))*

**Linux Kernel** — the vendor kernel (Raspberry Pi OS / OrangePi, or a robot's onboard compute)
we run on; we don't ship one. Our **Drivers** (`os/hal/drivers`, with per-board wiring in
`os/hal/board`) are userspace programs talking to it through GPIO/SPI/ALSA/V4L2; **Power
Management** is the foundation. *(see [kernel.md](kernel.md))*

## See also

[hal.md](hal.md) · [kernel.md](kernel.md) ·
[`DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md) ·
[`capabilities.md`](../../contract/capabilities.md)
