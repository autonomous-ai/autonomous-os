---
schema: autonomous.safety.v1

# Reachy Mini safety bounds. Conservative defaults until hardware spike
# measures actual limits. The motion driver (reachy_sdk) must enforce these;
# the OS policy layer reads them from this front matter.

motion:
  max_speed: 60              # deg/s ceiling — conservative for a Stewart platform head on a desk; tune after spike
  stop_always: true          # motion.stop/release are deterministic, never gated
  # max_accel: <int>         # (reserved) — no acceleration model yet

#thermal:
#  max_temp_c: 85            # (reserved) — measure Reachy's RPi thermal profile first
---

# SAFETY.md — Reachy Mini

The bounds contract for Reachy Mini. Same philosophy as Lamp: safety is below
the brain, enforced by the OS (Go/Python policy), not by prompting the agent.

## motion

- **`motion.stop` is immediate, deterministic, and always available.** It does
  not queue behind the gateway, the network, or any in-flight skill.
- The Stewart-platform head has 6 degrees of freedom and the body rotates 360°;
  both are speed-limited by the OS, not by the agent.
- The agent **does not drive raw actuator loops.** It requests poses and
  directions; the Pollen SDK + safety policy clamp to mechanical limits.
- No motion during a declared privacy-sensitive moment, during setup failure, or
  when the board reports a fault.
- Movement that could surprise a person is **explained** ("looking over here").

## audio

- No loud output without reason; respect quiet hours when declared.
- Never repeat secrets or overheard private speech back aloud.

## autonomy

- Proactive behavior is allowed within limits; **destructive actions are forbidden.**
- On network loss: stop agent-driven tracking, keep local idle presence + reflexes
  alive, start no new agent-driven motion.

## fail-safe states

| Condition | Behavior | Enforced |
|-----------|----------|----------|
| Network / gateway loss | Stop tracking, keep local reflexes, no new agent motion | **yes** |
| Board / driver fault | Disable the faulting capability, keep the rest, report health | **yes** |
| Setup incomplete | Setup / identity reflexes only | reserved |
