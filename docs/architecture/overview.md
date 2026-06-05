# Architecture

Autonomous is an open-source OS for **physical AI agents** — devices with cameras,
microphones, speakers, displays, motors, lights, and sensors. It is structured as a
**layered stack** modeled on Android: each layer exposes a defined interface to the
layer above and depends only on the layer below, so any layer can be replaced without
disturbing the others.

Two ideas are borrowed deliberately:

- **Vertical layering** (a Hardware Abstraction Layer above drivers) — from **Android**.
- **Horizontal generic-vs-board split** inside the lower layers (`drivers/` by
  subsystem, board specifics isolated) — from **Linux**.

![Autonomous architecture](autonomous-stack.svg)

## Terms

| Term | Definition |
|------|------------|
| **Behavior / Agent** | A user-facing capability (companion, guard, mood). Implemented as a `SKILL.md` and shaped by a `SOUL.md`. The "app." |
| **Agentic Gateway** | The reasoning engine (OpenClaw, Hermes, any LLM + skills + memory). The "brain." Reached over a WebSocket; swappable. |
| **Framework / Manager** | A stable on-device system service (intent, network, OTA, sensing, skills). The public API third parties build on. |
| **HAL** | The frozen, versioned **capability** interface (`audio.speak`, `motion.move`). What skills address — never hardware models. |
| **Capability** | A named, device-agnostic ability declared in `DEVICE.md` and `contract/capabilities.md`. |
| **Driver** | Code that talks to one piece of hardware (feetech servo, ws2812 LED). Internal and unstable by design. |
| **Platform / Board** | Per-board wiring (Pi 4/5, OrangePi). One profile per board. |
| **Kernel** | The **Linux kernel** (Raspberry Pi OS / OrangePi). Autonomous runs *on* it; it is not the gateway. See [kernel.md](kernel.md). |

## The layers

### Behaviors / Agents
*≈ Android System Apps.* What the device does — companion, guard, mood, scene. Each is
a `SKILL.md` the gateway can invoke; character comes from `SOUL.md`. First-party
behaviors are built on the **same public contract a third party gets** — no private back
door. If the first-party companion needs an API the contract doesn't expose, the
platform isn't real. *(Today under `os/core/resources/openclaw-skills`.)*

### Agentic Gateway — the brain
*No Android analog.* The reasoning engine — **OpenClaw**, **Hermes**, or any LLM + skills
+ memory runtime. Autonomous treats it as an abstract dependency over a WebSocket: it
reads the device's `SOUL.md` (who it is) and `SKILL.md` files (what it can do) and
decides what to do. Swappable by design — and where Autonomous's differentiated value
sits (the good default brain, memory, and character). *(`os/core/internal/openclaw`.)*

### Framework — the Managers
*≈ Android Java API Framework.* The stable system services, in Go: **intent** (fast local
commands with no round-trip to the brain), **network**/provisioning, **OTA**, **sensing**
event routing, the **skill** manager (install + capability matching — the `PackageManager`
analog), plus health and logging. This is the layer that makes Autonomous a *platform*
rather than an app. *(`os/core`.)*

### HAL — the capability interface
*≈ Android HAL.* The **frozen contract** between system and hardware: capabilities like
`audio.speak`, `motion.move`, `vision.snapshot` — named, versioned, stable. Skills and
agents address **capabilities, never hardware models**, so one skill runs on any body
that declares the capability. The Autonomous equivalent of the Linux syscall ABI.
Full detail in [hal.md](hal.md). *(`contract/`.)*

### Drivers
*≈ Linux kernel `drivers/`, by subsystem.* Code that talks to silicon — the **feetech**
servo bus, **ws2812** LED, **gc9a01** display, **camera**, audio **STT/TTS/VAD**.
Internal and unstable on purpose: when the HAL changes, every in-tree driver is fixed in
the same commit (the Linux model). *(`os/hal/lelamp/service/*`.)*

### Platform
*≈ Linux `arch/`, by board.* Per-board wiring — GPIO lines, PWM-vs-SPI LED transport,
touch lines — for **Pi 4/5** and **OrangePi**. One source of truth (`board_profile()`);
a new board is one entry, and a silicon swap is a *port*, not a rewrite.
*(`os/hal/lelamp/platform/board.py`.)*

### Safety + Power — the floor
*≈ Android Power Management (the cross-cutting bottom bar).* For a **physical** agent the
foundational concern is **safety**, so the e-stop, motion limits, thermal cutoffs, and
fail-safe behavior live *below the brain*, enforced by deterministic OS policy — never by
prompting the LLM. Note the geometry: `SOUL.md` is at the very top (mutable character),
`SAFETY.md` at the very bottom (immutable bounds). The thing wearing the personality can
never override the floor. *(`devices/<id>/SAFETY.md`.)*

## What's next

- [hal.md](hal.md) — the Hardware Abstraction Layer in detail
- [kernel.md](kernel.md) — what "kernel" means here (Linux, not the gateway)
- [`../../contract/DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md) — the device contract
- [`../../contract/capabilities.md`](../../contract/capabilities.md) — the capability vocabulary
