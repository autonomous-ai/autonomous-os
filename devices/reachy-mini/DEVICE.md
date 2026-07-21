---
schema: autonomous.device.v1
id: reachy-mini
name: Reachy Mini
type: desk_robot
manufacturer: Pollen Robotics
extends: _base
boards: [raspberry_pi_4, raspberry_pi_5]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio:      { routes: [audio, speaker, voice], required: true }
  vision:     { routes: [camera], required: true }
  sensing:    { routes: [sensing], required: false }
  presence:   { required: false }
  motion:     { routes: [servo], driver: reachy_sdk, required: true, safety: SAFETY.md#motion }
  expression: { routes: [emotion], required: true }
  system:     { routes: [system], required: true }
soul_ref:   SOUL.md
safety_ref: SAFETY.md
memory:     { backend: local }
---

# Reachy Mini

A **third-party** desk robot — Pollen Robotics' Reachy Mini Wireless — running
Autonomous. The third device ported to the OS and the first body from an external
manufacturer to run live (Go2-W was declaration-only).

## The point: Android playbook, proven

Lamp is Autonomous's own desk robot. Reachy Mini is **someone else's hardware**
running the same OS. Onboarding it was writing this `DEVICE.md`, a motion driver
wrapping Pollen's Python SDK, and a SAFETY.md — not a fork.

- **`motion` is a Stewart platform** (6-DOF head + 360° body + 2 antennas), not
  Feetech bus servos — yet a skill calling `motion.aim` runs on Reachy *and* on
  Lamp, because skills address capabilities, never hardware.
- It runs on **Reachy's onboard Raspberry Pi**, the same compute as Lamp.
- No `light` or `display` — Reachy expresses through head movement and antenna
  posture, not an LED ring or a screen.

## Body

A 28 cm desk robot: a Stewart-platform head (6-DOF parallel kinematics), a 360°
rotating body, two antenna ears (expression), an HD wide-angle camera, a 4-mic
array, and a 5W speaker. Compute is a Raspberry Pi 4/5 onboard. The body is
controlled via Pollen's Python SDK (`reachy_sdk`); the agent never addresses
hardware directly.

## What the agent should assume

- No LED ring — expression is through head tilt, antenna position, and voice.
- The body rotates 360° but there is no wheeled locomotion — it stays on the desk.
- Camera is wide-angle and fixed in the head; head movement steers the camera.
- Same privacy posture as Lamp: local-first, ask before sensitive sensing.
