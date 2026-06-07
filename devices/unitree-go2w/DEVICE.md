---
schema: autonomous.device.v1
id: unitree-go2w
name: Unitree Go2-W
type: mobile_robot
manufacturer: Unitree
extends: _base
boards: [unitree_go2w]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio:   { routes: [audio, speaker, voice], required: true }
  vision:  { routes: [camera, depth], required: true }
  sensing: { routes: [sensing], required: true }
  motion:  { routes: [locomotion], driver: unitree_sdk, required: false, safety: SAFETY.md#motion }
  system:  { routes: [system], required: true }
soul_ref:   autonomous://souls/companion
safety_ref: SAFETY.md
memory:     { backend: local }
---

# Unitree Go2-W

A **third-party** mobile robot — Unitree's wheeled quadruped — running Autonomous. We fitted
it with a camera, microphone, speaker, and a 3D depth camera; it hears you, sees the room in
3D, drives through the space, and acts.

## The point: any device, any manufacturer

This is the Android playbook. Lamp and Intern are Autonomous's own devices; the Go2-W is
**someone else's hardware** running the same OS — the way Android runs on Samsung, Pixel, and
the rest. Onboarding it was writing this `DEVICE.md` plus a driver, not a fork:

- **`motion` is locomotion**, and its driver is the **Unitree SDK**, not Feetech servos — yet a
  skill calling `motion.move` ("come here") runs on the Go2-W *and* on Lamp, because skills
  address capabilities, never hardware.
- It runs on **Unitree's onboard compute**, not a Raspberry Pi — a new board profile.
- `vision` adds a **depth** route (the 3D camera) for navigation.

Desk arm (Lamp) → desk cube (Intern) → a different vendor's mobile robot (Go2-W): one OS.
