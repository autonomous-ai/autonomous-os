---
schema: autonomous.device.v1
id: _base
name: Autonomous Base Device
type: base
abstract: true
boards: [raspberry_pi_4, raspberry_pi_5, orangepi_sun60]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio:  { routes: [audio, speaker, voice], required: true }
  system: { routes: [system], required: true }
memory: { backend: local }
---

# Base device

The minimum every Autonomous device inherits — the floor of
[`devices/contract/COMPATIBILITY.md`](../../devices/contract/COMPATIBILITY.md). It is `abstract: true`: it is
not a shippable device, it is the base a real device extends.

A concrete device's `ROBOT.md` adds the capabilities its body has (`vision`, `motion`,
`light`, `display`, `sensing`, …) on top of this base. The two reference devices show the range:

- **[Lamp](../lamp/)** — adds everything (the maximal device).
- **[Intern](../intern-v2/)** — adds `vision` + `sensing`, no actuation.

Every device declares `system` (health, setup, OTA) and at least one primary sense or output;
this base declares `audio`, the agent baseline. Inheritance keeps onboarding a new device to
"declare what's different," never a fork.
