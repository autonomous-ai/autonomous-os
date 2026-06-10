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
  presence:     { routes: [emotion, scene], required: true }
  motion:       { routes: [servo], driver: feetech, required: true, safety: SAFETY.md#motion }
  light:        { routes: [led], driver: ws2812, required: true, safety: SAFETY.md#light }
  display:      { routes: [display], driver: gc9a01, required: true }
  media:        { routes: [music], required: true }
  connectivity: { routes: [bluetooth], required: true }
  system:       { routes: [system], required: true }
soul_ref:   SOUL.md
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
`os/hal/board/board.py`; the agent never addresses hardware directly.

## What the agent should assume

- The user is likely physically near the device, in a private space.
- Camera and microphone are sensitive — prefer local processing, ask before new uses.
- Movement can surprise people. Move gently, legibly, and stop on command.
- Light and motion are communication channels, not decoration.

## Soul and memory references

`soul_ref` points at the character that inhabits this body. It resolves to a soul
artifact — a path read relative to this device folder (here, `SOUL.md`), or an
`http(s)://` URL the runtime downloads. A body with no `soul_ref` (e.g. Intern) keeps
the gateway's default soul. `memory` names the continuity layer by backend. `DEVICE.md`
describes the body; the soul is referenced here, not embedded in the front matter.
