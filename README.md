# Autonomous

**Autonomous is the open-source OS for physical AI agents.** It runs on edge devices
with cameras, microphones, speakers, displays, motors, lights, and sensors, and gives
an AI agent a body: it sees, hears, speaks, moves, senses, remembers, runs skills, and
updates itself — locally first.

**Autonomous Lamp** is the first reference device. **Intern** is the second. Anyone can
build a third.

> The brain is a swappable **agentic runtime** (OpenClaw, Hermes, or any LLM + skills +
> memory). Autonomous is everything else — the body, the skills, and the bounds.

## Reference devices

| Device | What it is | Declares |
|--------|-----------|----------|
| **Autonomous Lamp** | 5-DOF expressive desk robot | the maximal set — audio, vision, motion, light, display, sensing |
| **Autonomous Intern** | always-on desk agent | audio, vision, sensing — **no** motion or display |

Lamp and Intern run the **same OS image**. The only difference is which capabilities each
device's `DEVICE.md` declares. That is the whole thesis: a new device is a `DEVICE.md`,
not a fork.

## Architecture

Autonomous is a layered stack: each layer exposes an interface to the one above and
depends only on the one below, so any layer can be replaced without touching the others.
(The layering follows Android; the driver/board split follows Linux.)

![Autonomous architecture](docs/architecture/autonomous-stack.svg)

<details>
<summary>Same diagram as ASCII (for terminals)</summary>

```
  Skills              guard · mood · scene · habit
  Agentic Runtime     OpenClaw · Hermes — runs skills, embodies SOUL.md
  System Services     intent · network · OTA · sensing · skill manager   [os/core]
  HAL · Capabilities  audio · vision · motion · light · display          [frozen]
  Drivers             feetech · ws2812 · gc9a01 · camera · STT/TTS/VAD
  Board Support       Raspberry Pi 4/5 · OrangePi
  Linux Kernel        GPIO · SPI · I2C · ALSA · V4L2 · USB
  ──────────────────────────────────────────────────────────────────────
  Safety (the floor)  e-stop · motion limits · thermal · fail-safe
```

</details>

- **Skills** — what the device does (`guard`, `mood`, `scene`). Each is a `SKILL.md` the
  runtime invokes. A skill is an *ability*; the device's *character* is its `SOUL.md`.
- **Agentic Runtime** — OpenClaw, Hermes, or any LLM + skills + memory runtime. Runs the
  skills, embodies `SOUL.md`, decides what to act on. Swappable.
- **System Services** — the always-on Go daemon: intent, network, OTA, sensing routing,
  the skill manager, health. Runs with or without the runtime.
- **HAL · Capabilities** — the frozen, versioned interface. Skills call capabilities
  (`motion.move`), never hardware; a `DEVICE.md` declares which a body has.
- **Drivers** — each talks to one piece of hardware (feetech, ws2812, gc9a01, camera, audio).
- **Board Support** — per-board wiring for Pi 4/5 and OrangePi. A new board is one profile.
- **Linux Kernel** — the vendor kernel (Raspberry Pi OS / OrangePi) we run on. We don't ship one.
- **Safety** — the floor: e-stop, motion limits, thermal, fail-safe — deterministic, never the runtime.

📖 Full docs: [overview](docs/architecture/overview.md) · [HAL](docs/architecture/hal.md) · [kernel](docs/architecture/kernel.md)

## The Autonomous Physical Agent Standard

Every device is self-describing to both humans and the runtime, in four files:

| File | Role | Consumer |
|------|------|----------|
| `DEVICE.md` | the **body** — what hardware is present | the OS, at boot |
| `SKILL.md` | the **hands** — what it can do | the runtime |
| `SOUL.md` | the **self** — who it is | the runtime |
| `SAFETY.md` | the **bounds** — what it must never do | the OS (deterministic) |

The contract that governs them lives under [`contract/`](contract/) — see
[`DEVICE-SPEC.md`](contract/DEVICE-SPEC.md) and [`capabilities.md`](contract/capabilities.md).

## Repository layout

```
contract/         FROZEN — DEVICE-SPEC, capability vocabulary (the ABI third parties build on)
os/
  core/           Go system services: intent, network, OTA, sensing routing, runtime bridge
    web/          on-device setup + monitor UI (React)
  hal/lelamp/     Python hardware runtime — drivers + the capability host
    platform/     board profiles + declaration-driven capability mounting
devices/          per-device contracts: lamp/ (DEVICE · SOUL · SAFETY), intern/, examples/
companions/       lamp-buddy (macOS) · desktop-buddy
docs/  imager/  scripts/  hardware/
```

## Quick start

```bash
# Go system services (cross-compiled to linux/arm64 — Pi or OrangePi)
make lamp-build            # builds the system server (os/core)
make lamp-test             # go test ./...

# Hardware runtime (runs on the Pi or OrangePi)
cd os/hal/lelamp && uv sync
make lelamp-dev            # uvicorn reload on :5001
make lelamp-test           # pytest

# Web UI
make web-install && make web-dev
```

## API convention

All HTTP endpoints return `{"status": 1, "data": <payload>, "message": null}` on success
and `{"status": 0, "data": null, "message": "error"}` on failure.

## Governance & license

Autonomous (the OS) is **Apache 2.0** and fully open. Premium souls, the memory continuity
service, Grid inference, and the skill store ship separately — open core, commercial
services on top. The project is BDFL-governed. See [`GOVERNANCE.md`](GOVERNANCE.md),
[`CONTRIBUTING.md`](CONTRIBUTING.md), and [`MAINTAINERS`](MAINTAINERS).

Build an **Autonomous-compatible** device: write a `DEVICE.md`, implement any missing
drivers against the HAL contract, ship a `SOUL.md`. You never fork the OS.
