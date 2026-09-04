---
schema: autonomous.safety.v1

# Reachy Mini safety bounds. Conservative defaults until hardware spike
# measures actual limits. The motion driver (reachy_sdk) must enforce these;
# the OS policy layer reads them from this front matter.

motion:
  max_speed: 800             # deg/s head-rotation ceiling; above everything Pollen's own move library demands (max 766) — see notes
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
- `max_speed` is applied two ways, because the driver owns only one of them.
  `min_move_duration` stretches a requested move (aim/nudge/goto). A **recorded
  move** is streamed by the Pollen daemon with HAL outside the loop, so it
  cannot be slowed mid-play: it is scanned before playback and **refused** if
  its peak head rotation exceeds the ceiling. Refusal applies to every source,
  official library included — an official move tripping the gate is evidence
  the number here is wrong, not a reason to excuse the move.
- **Where 800 comes from, and what it is not.** It is not a measurement of
  what the hardware tolerates — that needs a robot, and this number must be
  revisited once one is available. It is derived from the only evidence
  obtainable without one: all 85 moves in
  `pollen-robotics/reachy-mini-emotions-library` were scanned with the same
  function the gate uses, giving a median peak of 111 deg/s, a p90 of 293, and
  a maximum of 766 (`wake-mini-up`, with `dying1` at 760). A ceiling below that
  would refuse moves the vendor ships and presumably considers safe on their
  own robot, so the declared bound sits just above the vendor's own ceiling.
  The gate therefore catches content faster than anything Pollen publishes —
  which is exactly the case it exists for, a move loaded from a stranger's Hub
  dataset. The previous value, 60, was a placeholder ("tune after spike") and
  would have refused 63 of the 85 official moves.
- Head **translation** (`head_x/y/z`, millimetres) is not bounded: `max_speed`
  is a deg/s ceiling, and no translation limit is declared. Not enforced means
  not claimed.
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
