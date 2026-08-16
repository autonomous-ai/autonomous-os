# Unitree Go2-W

> **Status: declaration-only reference — not a supported runtime target.** This
> repository does not contain a Unitree board profile, Unitree SDK driver, or HAL
> locomotion/depth route. No Unitree hardware is claimed as validated here.

**The intended Autonomous port for someone else's robot.** The Go2-W is Unitree's
wheeled quadruped — third-party hardware that a future Autonomous port could equip
with a camera, microphone, speaker, and 3D depth camera.

<p align="center">
  <img src="images/go2-w.webp" alt="Unitree Go2-W" width="480">
</p>

## Why it matters — the Android playbook

Android runs on Samsung, Pixel, and a hundred other vendors' phones. Autonomous is the same
idea for physical agents: Lamp and Intern are *our* devices; the **Go2-W declaration** shows
the intended contract for a different manufacturer's robot. A real port still needs a board
profile, driver, HAL routes, and runtime validation:

- **`motion` would be locomotion**, driven by the **Unitree SDK**, not Feetech servos.
- It would run on **Unitree's onboard compute**, requiring a new board profile.
- A **depth** vision route would make the desk OS suitable for a roaming agent.

The declaration demonstrates the extension point; it does not implement the port.

## Capabilities

audio, vision (+ depth), motion (locomotion), sensing. Declared in [`ROBOT.md`](ROBOT.md);
bounds in [`SAFETY.md`](SAFETY.md) — mobile safety is non-negotiable.

## Status

Reference declaration only — no hardware validation or production support.

## For developers

- [`ROBOT.md`](ROBOT.md) · [`SAFETY.md`](SAFETY.md) · [`SOUL.md`](SOUL.md)
- [Architecture](../../docs/architecture/overview.md)
