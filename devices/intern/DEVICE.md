---
schema: autonomous.device.v1
id: autonomous-intern
name: Autonomous Intern
type: desk_agent
boards: [raspberry_pi_4, raspberry_pi_5, orangepi_sun60]
gateway:
  default: openclaw
  protocol: websocket
capabilities:
  audio:   { routes: [audio, speaker, voice], required: true }
  vision:  { routes: [camera], required: false }
  sensing: { routes: [sensing], required: false }
  system:  { routes: [system], required: true }
soul_ref:   autonomous://souls/intern
safety_ref: SAFETY.md
memory:     { backend: local }
---

# Autonomous Intern

The deliberate inverse of Lamp: an always-on desk agent with voice and ambient sensing,
but **no actuation and no display**. Intern proves the platform is real — it is not a
fork of Lamp. It is the same OS image declaring fewer capabilities.

## What makes Intern "not Lamp"

Exactly two lines: Lamp declares `motion` and `display`; Intern does not. The OS boots
the same runtime, mounts only the audio/vision/sensing/system routes Intern declares,
and never brings up servo or display drivers. There is no `if device == intern`
anywhere in the OS.

Intern v2 adds mic + speaker — capabilities Lamp already has — so the two devices share
their entire audio/voice stack verbatim. That overlap is the whole argument for one OS.

## What the agent should assume

- No body to move and no screen to draw on — presence is voice; light is unavailable.
- The job is agentic work (mail, calendar, tasks, research), not expressive companionship.
- Same privacy posture as Lamp: local-first, ask before sensitive sensing.
