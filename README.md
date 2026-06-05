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

## Architecture

Autonomous is a layered stack, modeled on Android's. Each layer talks only to the
one below it through a defined interface, so any layer can be replaced without
disturbing the rest. Two ideas are borrowed deliberately: the **vertical layering**
(a Hardware Abstraction Layer sitting above drivers) is Android's; the **horizontal
generic-vs-board split** inside the lower layers is Linux's (`drivers/` organized by
subsystem, `arch/` by board). Reading top to bottom:

```
┌──────────────────────────────────────────────────────────────────────┐
│  BEHAVIORS / AGENTS                          ≈ Android "System Apps"   │
│  ┌──────────┐ ┌───────┐ ┌──────┐ ┌───────┐ ┌──────────┐               │
│  │companion │ │ guard │ │ mood │ │ scene │ │ wellness │      …        │
│  └──────────┘ └───────┘ └──────┘ └───────┘ └──────────┘               │
└──────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────┐
│  AGENTIC GATEWAY — the swappable brain            (no Android analog)  │
│  ┌──────────┐ ┌────────┐    reads SOUL.md + SKILL.md over WebSocket    │
│  │ OpenClaw │ │ Hermes │    …any LLM + skills + memory runtime         │
│  └──────────┘ └────────┘                                              │
└──────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────┐
│  FRAMEWORK — Managers  ·  os/core (Go)       ≈ Android Java API FW     │
│  ┌────────┐ ┌─────────┐ ┌─────┐ ┌─────────┐ ┌───────┐ ┌────────────┐  │
│  │ intent │ │ network │ │ OTA │ │ sensing │ │ skill │ │ health/log │  │
│  └────────┘ └─────────┘ └─────┘ └─────────┘ └───────┘ └────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────┐
│  HAL — capability interface  ·  contract/ (FROZEN)    ≈ Android HAL    │
│  ┌───────┐ ┌────────┐ ┌────────┐ ┌───────┐ ┌─────────┐ ┌──────────┐   │
│  │ audio │ │ vision │ │ motion │ │ light │ │ display │ │ presence │   │
│  └───────┘ └────────┘ └────────┘ └───────┘ └─────────┘ └──────────┘   │
└──────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────┐
│  DRIVERS  ·  os/hal/lelamp (by subsystem)    ≈ Linux kernel drivers/   │
│  ┌─────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌───────────────┐       │
│  │ feetech │ │ ws2812 │ │ gc9a01 │ │ camera │ │  STT/TTS/VAD  │       │
│  └─────────┘ └────────┘ └────────┘ └────────┘ └───────────────┘       │
│  PLATFORM  ·  platform/board.py (by board)        ≈ Linux arch/       │
│  ┌────────────────┐ ┌────────────────┐ ┌────────────────────┐         │
│  │ raspberry_pi_5 │ │ raspberry_pi_4 │ │ orangepi_sun60  …  │         │
│  └────────────────┘ └────────────────┘ └────────────────────┘         │
└──────────────────────────────────────────────────────────────────────┘
╔══════════════════════════════════════════════════════════════════════╗
║  SAFETY + POWER  ·  SAFETY.md   (deterministic, always on, below the   ║
║  brain)     e-stop · motion limits · thermal · fail-safe states        ║
╚══════════════════════════════════════════════════════════════════════╝
```

### Behaviors / Agents
*Android analog: System Apps.* What the device actually does — a calm desk
**companion**, **guard** mode, **mood** lighting, **scene** control. Each is a
`SKILL.md` the gateway can invoke; a device's character is set by its `SOUL.md`.
Crucially, these are built on the **same public contract a third party gets** — no
private back door. If the first-party companion needs an API the contract doesn't
expose, the platform isn't real. *(Today under `os/core/resources/openclaw-skills`;
lifting `skills/` + `souls/` to the top level is a planned follow-up.)*

### Agentic Gateway — the brain
*No Android analog — this is the one tier a phone OS doesn't have.* The reasoning
engine: **OpenClaw**, **Hermes**, or any LLM + skills + memory runtime. Autonomous
treats it as an **abstract dependency** reached over a WebSocket; it reads the
device's `SOUL.md` (who it is) and the available `SKILL.md` files (what it can do)
and decides what to do. Swappable by design — and the layer where Autonomous's
value concentrates (the good default brain, memory, and character are the part that
isn't commodity). *(`os/core/internal/openclaw`.)*

### Framework — the Managers
*Android analog: the Java API Framework's managers (Activity, Package, Notification…).*
The stable system services third parties build against, in Go: **intent** (fast local
commands, no round-trip to the brain), **network** + provisioning, **OTA** updates,
**sensing** event routing, the **skill** manager (install + capability matching — the
`PackageManager` equivalent), plus health and logging. This is the layer that makes
Autonomous a *platform* rather than an app. *(`os/core`.)*

### HAL — the capability interface
*Android analog: the Hardware Abstraction Layer.* The **frozen contract** between the
system and the hardware: capabilities like `audio.speak`, `motion.move`,
`vision.snapshot` — named, versioned, and stable. Skills and agents address
**capabilities, never hardware models**, so one skill runs on any body that declares
the capability. This is the Autonomous equivalent of the Linux syscall ABI: stable
above, free to churn below. *(`contract/capabilities.md` + `contract/DEVICE-SPEC.md`.)*

### Drivers
*Linux analog: kernel `drivers/`, organized by subsystem.* The code that actually
talks to silicon — the **feetech** servo bus, **ws2812** LED, **gc9a01** display,
**camera**, and the audio **STT/TTS/VAD** pipeline. Internal and unstable on purpose:
when a HAL change ripples, every in-tree driver is fixed in the same commit (the Linux
model — which is why Linux supports more hardware out of the box than anything).
*(`os/hal/lelamp/service/*`.)*

### Platform
*Linux analog: `arch/`, organized by board.* Per-board wiring — which GPIO the button
is on, whether the LED is PWM or SPI, the touch lines — for **Raspberry Pi 4/5** and
**OrangePi**. One source of truth (`board_profile()`); a new board is one entry, and a
silicon swap (e.g. to a cheaper custom board) is a *port*, not a rewrite. *(`os/hal/lelamp/platform/board.py`.)*

### Safety + Power — the floor
*Android analog: Power Management, the cross-cutting bar at the very bottom.* For a
**physical** agent the foundational concern isn't battery, it's **safety** — so the
e-stop, motion limits, thermal cutoffs, and fail-safe behavior live *below the brain*,
enforced by deterministic OS policy, never by prompting the LLM. (Guard mode in this
codebase was already rebuilt to deliver alerts deterministically because routing them
through the agent was unreliable — that's the template.) Note the geometry: `SOUL.md`
sits at the very top (mutable character), `SAFETY.md` at the very bottom (immutable
bounds). The thing wearing the personality can never override the floor.
*(`devices/<id>/SAFETY.md`.)*

### Android ↔ Autonomous, at a glance

| Android layer | Autonomous layer | What changed |
|---|---|---|
| System Apps | Behaviors / Agents | apps → skills + souls, on the public contract |
| *(none)* | **Agentic Gateway** | the one new tier — the swappable brain |
| Java API Framework | Framework / Managers | same idea, in Go (`os/core`) |
| Hardware Abstraction Layer | HAL — capability contract | frozen + versioned (`contract/`) |
| Linux kernel drivers | Drivers (by subsystem) | same model (`os/hal`) |
| *(arch/)* | Platform (by board) | Linux's board split, surfaced explicitly |
| Power Management | **Safety + Power** | safety is the floor for a device with a body |

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

```
contract/         FROZEN — DEVICE-SPEC, capability vocabulary (the ABI 3rd parties build on)
os/
  core/           Go framework: managers, gateway bridge, OTA, network, sensing routing
    web/          on-device setup + monitor UI (React)
  hal/lelamp/     Python hardware runtime — drivers + the capability host
    platform/     board profiles (Linux arch seam) + capability mounting (Android seam)
devices/          per-device contracts: lamp/ (DEVICE · SOUL · SAFETY), intern/, examples/
companions/       lamp-buddy (macOS) · desktop-buddy
docs/  imager/  scripts/  hardware/
```

Reference devices live in `devices/`; the OS runtime in `os/`. Splitting the HAL
into `runtime/ drivers/ platform/` and lifting `skills/` + `souls/` to the top level
are planned follow-ups.

## Quick start

```bash
# Go framework (cross-compiled to linux/arm64 — Pi or OrangePi)
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

All HTTP endpoints return `{"status": 1, "data": <payload>, "message": null}` on
success and `{"status": 0, "data": null, "message": "error"}` on failure.

## Governance & license

Autonomous (the OS) is **Apache 2.0** and fully open. Premium souls, the memory
continuity service, Grid inference, and the skill store ship separately — the AOSP/GMS
split. The project is BDFL-governed. See [`GOVERNANCE.md`](GOVERNANCE.md),
[`CONTRIBUTING.md`](CONTRIBUTING.md), and [`MAINTAINERS`](MAINTAINERS).

Build an **Autonomous-compatible** device: write a `DEVICE.md`, implement any missing
drivers against the HAL contract, ship a `SOUL.md`. You never fork the OS.
