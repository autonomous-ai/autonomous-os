# Unitree Go2-W

![Unitree Go2-W](images/go2-w.webp)

**Autonomous on someone else's robot.** The Go2-W is Unitree's wheeled quadruped — a
third-party manufacturer's hardware — running the Autonomous OS. We fitted it with a camera,
microphone, speaker, and a 3D depth camera; it hears you, sees the room in 3D, drives through
the space, and acts. A true mobile agent, not a remote-controlled toy.

## Why it matters — the Android playbook

Android runs on Samsung, Pixel, and a hundred other vendors' phones. Autonomous is the same
idea for physical agents: Lamp and Intern are *our* devices; the **Go2-W is a different
manufacturer's robot** running the identical OS. Bringing it up was writing a `DEVICE.md` plus
a driver — never a fork:

- **`motion` is locomotion**, driven by the **Unitree SDK**, not Feetech servos — yet a skill
  that calls `motion.move` ("come here") runs on the Go2-W *and* on Lamp, because skills
  address capabilities, never hardware.
- It runs on **Unitree's onboard compute**, not a Raspberry Pi — a new board profile.
- Add a **depth** vision route and the desk OS becomes a roaming agent.

The thesis at its strongest: a desk arm (Lamp), a desk cube (Intern), and **a robot from a
different vendor** (Go2-W) — one OS, three `DEVICE.md` files.

## Capabilities

audio, vision (+ depth), motion (locomotion), sensing. Declared in [`DEVICE.md`](DEVICE.md);
bounds in [`SAFETY.md`](SAFETY.md) — mobile safety is non-negotiable.

## Status

Pilot — validated on Unitree hardware.

## For developers

- [`DEVICE.md`](DEVICE.md) · [`SAFETY.md`](SAFETY.md) · [`SOUL.md`](SOUL.md)
- [Architecture](../../docs/architecture/overview.md)
