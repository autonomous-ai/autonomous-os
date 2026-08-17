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
soul_ref:   SOUL.md
safety_ref: SAFETY.md
memory:     { backend: local }
---

# Unitree Go2-W

> **Status: declaration-only reference.** This repository does not ship a Unitree
> board profile, SDK driver, or HAL locomotion/depth routes. It is not a supported
> deployment target and must not be represented as a running Autonomous device.

This is the intended declaration for a **third-party** mobile robot — Unitree's
wheeled quadruped. A future port would fit it with a camera, microphone, speaker,
and 3D depth camera so it can hear, see the room in 3D, and drive through space.

## The point: any device, any manufacturer

This is the Android playbook. Lamp and Intern are Autonomous's own devices; the Go2-W is
**someone else's hardware** running the same OS — the way Android runs on Samsung, Pixel, and
the rest. Onboarding it was writing this `ROBOT.md` plus a driver, not a fork:

- **`motion` would be locomotion**, with a Unitree SDK driver rather than Feetech
  servos. A port can then map capability-level motion skills to the body.
- It would run on **Unitree's onboard compute**, requiring a new board profile.
- `vision` would add a **depth** route (the 3D camera) for navigation.

It is a contract/reference example for extending the OS to a different vendor's
mobile robot, not evidence that this port exists.
