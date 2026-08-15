---
schema: autonomous.safety.v1

# Mock-body bounds. Deliberately strict: a clamp that fires here must fire on
# real hardware too, so the mock body is a place to prove the gate, not a place
# to escape it.

motion:
  max_speed: 60              # deg/s ceiling
  stop_always: true          # stop/release are deterministic, never gated
---

# SAFETY.md — Mock Body

The mock body has no mass, no motors and nothing to hurt. It still carries
bounds, for one reason: the safety gate is a pure function of this file
(`hal/safety/policy.py`), so the mock body is where that function can be tested
without a robot.

## motion

- `max_speed: 60` deg/s — the same clamp shape a real body declares. A request
  above it is stretched in time, never truncated.
- `stop_always: true` — stop and release are plain code, never routed through
  the brain.
- Note what is honest here and everywhere else in this tree: `release` travels
  to a rest pose *before* cutting torque, and nothing aborts a move already in
  flight. The mock driver reproduces that behavior deliberately rather than
  pretending a real stop exists.

## What is not declared

No `light`, `audio` or `thermal` bounds, because this body declares none of
those capabilities. A bound with nothing behind it is worse than an absent one.
