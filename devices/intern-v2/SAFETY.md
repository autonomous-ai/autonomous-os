---
schema: autonomous.safety.v1
light:
  max_brightness: 120        # 0–255 ceiling; the LED route clamps any higher request
  # Quiet hours lower the ceiling on real wall-clock time (the device runs all day;
  # this is not a nightlight). 22:00–07:00 → ring dims to 40, agent-independent.
  quiet_hours: { start: "22:00", end: "07:00", max_brightness: 40 }
---

# SAFETY.md — Autonomous Intern

The bounds contract: `ROBOT.md` says what the body *can* do; `SAFETY.md` says what it
*must never* do, enforced deterministically by the OS — not by prompting the agent
(see `devices/contract/SAFETY-SPEC.md`, and `devices/lamp/SAFETY.md` for the fuller reference).

Intern declares fewer capabilities than Lamp, so it carries fewer bounds: it has **no
motion** (nothing to speed-limit) and audio is **voice-only** (no `media`/music route, so
no loud discretionary output to suppress). Only the LED ring (`light`) is governed here.

## light

The LED ring is calm by default — no sudden full-brightness output. The runtime clamps
every request to `max_brightness: 180`, and inside quiet hours (22:00–07:00) the ceiling
drops to 40, independent of the agent. (Values mirror Lamp as sensible defaults — tunable
in the front matter above.)

## fail-safe

On a board/driver fault the faulting capability is disabled (its routes return `503`)
while the rest keep running and health is reported — the same per-capability isolation
Lamp uses. With no motion or camera, Intern has no in-flight tracking to stop on a
network/gateway loss; local voice reflexes stay available.
