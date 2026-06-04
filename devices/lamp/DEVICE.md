---
schema: autonomous.device.v1
id: autonomous-lamp
name: Autonomous Lamp
type: desk_robot
boards: [raspberry_pi_4, raspberry_pi_5, orangepi_sun60]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio:        { routes: [audio, speaker, voice], required: true }
  vision:       { routes: [camera], required: true }
  sensing:      { routes: [sensing], required: true }
  presence:     { routes: [emotion, scene], required: false }
  motion:       { routes: [servo], driver: feetech, required: false, safety: SAFETY.md#motion }
  light:        { routes: [led], driver: ws2812, required: false, safety: SAFETY.md#light }
  display:      { routes: [display], driver: gc9a01, required: false }
  media:        { routes: [music], required: false }
  connectivity: { routes: [bluetooth], required: false }
  system:       { routes: [system], required: true }
soul_ref:   autonomous://souls/lamp-companion
safety_ref: SAFETY.md
memory:     { backend: local }
---

# Autonomous Lamp

The maximal reference device — a 5-DOF desk robot that sees, hears, speaks, moves,
glows, and displays expression. Lamp exists to exercise every subsystem of the OS: if
a capability works on Lamp, it works.

## Body

A weighted base, a 5-servo articulated arm (Feetech bus servos over `/dev/ttyACM0`), a
warm LED ring head (WS2812), a round GC9A01 display, a camera, a microphone, and a
speaker. Compute is a Raspberry Pi 4/5 or OrangePi (sun60). The body is wired per
`os/hal/platform/<board>`; the agent never addresses hardware directly.

## What the agent should assume

- The user is likely physically near the device, in a private space.
- Camera and microphone are sensitive — prefer local processing, ask before new uses.
- Movement can surprise people. Move gently, legibly, and stop on command.
- Light and motion are communication channels, not decoration.

## Closed-layer references

`soul_ref` and `memory` point at the character and continuity layers **by name** — they
are not embedded here. `DEVICE.md` is open and describes the body; the soul that
inhabits it ships separately (see GOVERNANCE.md § Open vs. closed).
