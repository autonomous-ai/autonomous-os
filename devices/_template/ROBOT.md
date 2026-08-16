---
schema: autonomous.device.v1
id: my-robot
name: My Robot
type: mobile_robot
boards: [raspberry_pi_5]
gateway: { default: openclaw }
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true }
  vision: { routes: [camera], driver: opencv, required: true }
  motion: { routes: [servo], driver: my_sdk, required: true, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: SOUL.md
safety_ref: SAFETY.md
---

# My Robot

A starting point for a port. Copy it with `make new-device NAME=<id>`, then:

1. Declare only the capabilities your hardware has (`devices/contract/capabilities.md`
   has the 13 names; the OS mounts nothing you don't declare).
2. Add a `boards.json` entry for your compute and list it under `boards`.
3. Ship a `SAFETY.md` — anything that declares `motion` must, and `make cts`
   fails until it does.
4. Write a `SOUL.md` — who this robot is.

Full path: `docs/bring-your-own-robot.md`.
