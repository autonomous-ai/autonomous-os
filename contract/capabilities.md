# Capability Vocabulary — `autonomous.capabilities.v1`

The frozen namespace of physical capabilities. A `DEVICE.md` declares which groups a
body has; a `SKILL.md` declares which it needs. Names here are an ABI — once published,
a name is never removed or repurposed (see `DEVICE-SPEC.md` § Versioning).

A capability group maps to one or more HAL routes under `os/hal/`. The route is the
typed HTTP surface (consumer: the system); the capability is the agent-facing name
(consumer: the LLM). **Skills and agents address capabilities, never routes or hardware
models.**

| Group | Capabilities | HAL routes (today) | Privacy | Safety |
|-------|--------------|--------------------|---------|--------|
| `audio` | `audio.speak`, `audio.listen` | audio, speaker, voice | microphone | loud-output |
| `vision` | `vision.snapshot`, `vision.stream` | camera | camera | — |
| `sensing` | `sensing.presence`, `sensing.motion`, `sensing.sound`, `sensing.light` | sensing | ambient | — |
| `presence` | `presence.face`, `presence.emotion` | emotion, scene | biometric | — |
| `motion` | `motion.move`, `motion.track`, `motion.stop` | servo | — | motion |
| `light` | `light.paint`, `light.effect` | led | — | bright-output |
| `display` | `display.render` | display | — | — |
| `media` | `media.play` | music | — | loud-output |
| `connectivity` | `connectivity.bluetooth` | bluetooth | — | — |
| `system` | `system.health`, `system.ota`, `system.network`, `system.setup` | system | — | — |

## Rules

- **Address capabilities, not models.** A skill says `motion.move`, never "Feetech
  servo ID 3" — that lets the same skill run on any body that declares `motion`.
- **Degrade gracefully.** A skill that lists a capability as optional must run without
  it. A skill that requires a capability simply does not load on a device that lacks it.
- **`*.stop` is sacred.** Any group with a `safety` class must expose an immediate,
  deterministic stop. `motion.stop` never routes through the LLM (see `SAFETY.md`).
