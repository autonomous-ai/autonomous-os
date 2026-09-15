---
schema: autonomous.device.v1
id: stackchan
name: Stack-chan (experimental remote motion)
type: desk_robot
boards: [host]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  motion: { routes: [servo], driver: stackchan, required: true, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: SOUL.md
safety_ref: SAFETY.md
memory: { backend: local }
---

# Stack-chan experimental host profile

HAL runs on a development computer; an ESP32 body initiates an authenticated
WSS connection to HAL. The `host` board has no local actuator or GPIO wiring.
This profile exposes motion and system routes only. It does not declare
camera, microphone, speaker, display, LEDs, sensing, or recorded animations.

This is a local integration profile, not a qualified device or an OTA release
target. It lives under `_experimental` because a motion-only integration does
not meet the full device compatibility contract's audio-or-vision requirement.
It deliberately does not inherit `_base` or claim media it cannot provide.

Read [runtime instructions](docs/runtime.md) or the
[Vietnamese version](docs/vi/runtime_vi.md). Startup listens for the body and
does not issue a wake animation or a movement command.
