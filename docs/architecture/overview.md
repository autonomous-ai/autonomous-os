# Architecture

Autonomous is a layered stack. Each layer exposes an interface to the layer above and
depends only on the one below, so any layer can be replaced without touching the others.
(The layering follows Android; the driver/board split follows Linux.)

![Autonomous architecture](autonomous-stack.svg)

## Layers

**Skills** — what the device does: `guard`, `mood`, `scene`, `habit`. Each is a `SKILL.md`
the runtime invokes. A skill is an *ability*; it is not the device's *character* — that's
its `SOUL.md`. First-party skills use the same public contract a third party gets.
*(`os/core/resources/openclaw-skills`)*

**Agentic Runtime** — OpenClaw, Hermes, or any LLM + skills + memory runtime. It runs the
skills, embodies the device's `SOUL.md`, and decides what to act on. Swappable — and where
Autonomous's differentiated value (the default brain, memory, character) lives.
*(`os/core/internal/openclaw`)*

**System Services** — the always-on device daemon, in Go: `intent` (fast local commands),
`network`, `OTA`, `sensing` routing, the `skill` manager, health and logging. Runs with or
without the runtime. *(`os/core`)*

**HAL — Capabilities** — the frozen, versioned interface between software and hardware:
`audio.speak`, `motion.move`, `vision.snapshot`. Skills call capabilities, never hardware
models, so one skill runs on any body that declares the capability. A device's `DEVICE.md`
declares which it has; the runtime mounts only those. *(`contract/` — see [hal.md](hal.md))*

**Drivers** — each talks to one piece of hardware: the feetech servo, ws2812 LED, gc9a01
display, camera, the audio STT/TTS/VAD pipeline. *(`os/hal/lelamp/service`)*

**Board Support** — per-board wiring (GPIO lines, PWM-vs-SPI LED, touch) for Raspberry Pi
4/5 and OrangePi. One profile per board; swapping silicon is a port, not a rewrite.
*(`os/hal/lelamp/platform/board.py`)*

**Linux Kernel** — the vendor kernel (Raspberry Pi OS / OrangePi) we run on. We don't ship
a kernel; drivers use its userspace interfaces (GPIO, SPI, ALSA, V4L2). *(see [kernel.md](kernel.md))*

**Safety** — the floor. The e-stop, motion limits, thermal cutoff, and fail-safe behavior
are enforced by deterministic policy, never by the runtime. `SOUL.md` is at the top (mutable
character); `SAFETY.md` is at the bottom (immutable bounds) — character can't override the
floor. *(`devices/<id>/SAFETY.md`)*

## See also

[hal.md](hal.md) · [kernel.md](kernel.md) ·
[`DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md) ·
[`capabilities.md`](../../contract/capabilities.md)
