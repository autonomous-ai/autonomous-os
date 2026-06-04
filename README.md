# Autonomous

**Autonomous is the open-source OS for physical AI agents.** It runs on edge devices
with cameras, microphones, speakers, displays, motors, lights, and sensors, and gives
an AI agent a body: it sees, hears, speaks, moves, senses, remembers, runs skills, and
updates itself — locally first.

**Autonomous Lamp** is the first reference device. **Intern** is the second. Anyone can
build a third.

> The brain is a pluggable agentic gateway (OpenClaw, Hermes, or any LLM + skills +
> memory runtime). Autonomous is everything else — the body, the hands, and the bounds.

## Reference devices

| Device | What it is | Declares |
|--------|-----------|----------|
| **Autonomous Lamp** | 5-DOF expressive desk robot | the maximal set — audio, vision, motion, light, display, sensing |
| **Autonomous Intern** | always-on desk agent | audio, vision, sensing — **no** motion or display |
| **Sphere** + more | in the pipeline | — |

Lamp and Intern run the **same OS image**. The only difference is which capabilities
each device's `DEVICE.md` declares. That is the whole thesis: a new device is a
`DEVICE.md`, not a fork.

## The stack

```
Behaviors / Agents      companion · guard · mood — built as skills, configured by SOUL.md
        ▲
Agentic Gateway         OpenClaw · Hermes — the swappable brain (reads SOUL.md + SKILL.md)
        ▲
Framework (managers)    intent · network · OTA · sensing · skills — the stable public API   [os/core]
        ▲
HAL (capability iface)  audio · vision · motion · light · display · presence              [os/hal — FROZEN contract]
        ▲
Drivers (by subsystem)  feetech · ws2812 · gc9a01 · camera · audio                         [os/hal/drivers]
Platform (by board)     raspberry_pi_5 · orangepi_sun60 · …                                [os/hal/platform]
        ▲
Safety + Power          deterministic floor — e-stop below the brain, always on            [SAFETY.md]
```

Vertical layering is borrowed from Android (HAL above drivers); the horizontal
generic-vs-board split is borrowed from Linux (`drivers/` by subsystem, `arch/` by
board). The frozen `contract/` is the Autonomous equivalent of the Linux syscall ABI:
stable for devices and skills, churning underneath.

## The Autonomous Physical Agent Standard

Every device is self-describing to both humans and agents, in four files:

| File | Role | Consumer |
|------|------|----------|
| `DEVICE.md` | the **body** — what hardware is present | the OS, at boot |
| `SKILL.md` | the **hands** — what it can do | the gateway/LLM |
| `SOUL.md` | the **self** — who it is | the gateway/LLM |
| `SAFETY.md` | the **bounds** — what it must never do | the OS (deterministic) |

The contract that governs them lives under [`contract/`](contract/). See
[`contract/DEVICE-SPEC.md`](contract/DEVICE-SPEC.md) and
[`contract/capabilities.md`](contract/capabilities.md).

## Repository layout

> **In transition.** This repo was forked from `autonomous-lamp` and is being
> restructured into the platform layout below. Today the runtime still lives in `lamp/`
> (Go) and `lelamp/` (Python); those move to `os/core` and `os/hal` in the restructure
> (see the migration plan). Build commands below still reference the current paths.

```
contract/         FROZEN — DEVICE-SPEC, capability vocabulary (the ABI 3rd parties build on)
os/               the OS
  core/           Go framework: managers, gateway bridge, OTA, network, sensing routing   (← lamp/)
  hal/
    runtime/      capability host — mounts what DEVICE.md declares                          (← lelamp/server)
    drivers/      by subsystem: motion, audio, vision, light, display, sensing             (← lelamp/service/*)
    platform/     by board: raspberry_pi_5, orangepi_sun60, …                              (← consolidated)
  web/  imager/
devices/          per-device contracts: lamp/, intern/, examples/minimal/
skills/           the SKILL.md app layer                                                    (← resources/openclaw-skills)
souls/            character packs (default open; premium ships separately)
docs/             architecture, flows, board bring-up (EN + VI)
companions/       lamp-buddy (macOS), desktop-buddy
```

## Quick start (current paths, pre-restructure)

```bash
# Go framework (cross-compiled to linux/arm64 — Pi or OrangePi)
make lamp-build            # builds the system server
make lamp-test             # go test ./...

# Hardware runtime (runs on the Pi or OrangePi)
cd lelamp && uv sync
make lelamp-dev            # uvicorn reload on :5001
make lelamp-test           # pytest

# Web UI
make web-install && make web-dev
```

## API convention

All HTTP endpoints return `{"status": 1, "data": <payload>, "message": null}` on
success and `{"status": 0, "data": null, "message": "error"}` on failure.

## Governance & license

Autonomous (the OS) is **Apache 2.0** and fully open. Premium souls, the memory
continuity service, Grid inference, and the skill store ship separately — the AOSP/GMS
split. The project is BDFL-governed. See [`GOVERNANCE.md`](GOVERNANCE.md),
[`CONTRIBUTING.md`](CONTRIBUTING.md), and [`MAINTAINERS`](MAINTAINERS).

Build an **Autonomous-compatible** device: write a `DEVICE.md`, implement any missing
drivers against the HAL contract, ship a `SOUL.md`. You never fork the OS.
