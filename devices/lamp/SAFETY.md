---
schema: autonomous.safety.v1
light:
  max_brightness: 180        # 0–255 daytime ceiling; the LED route clamps any higher request
  # Quiet hours lower the ceiling on real wall-clock time (the device runs all
  # day; this is not a nightlight). 22:00–07:00 → ring dims to 40, agent-independent.
  quiet_hours: { start: "22:00", end: "07:00", max_brightness: 40 }
audio:
  # No loud discretionary output (music) during quiet hours; spoken replies still allowed.
  quiet_hours: { start: "22:00", end: "07:00" }
motion:
  max_speed: 120             # deg/s ceiling; the servo route stretches a move's duration so no joint exceeds it
  stop_always: true          # motion.stop/release are deterministic and never gated
  # max_accel: <int>         # reserved
---

# SAFETY.md — Autonomous Lamp

The bounds contract. `DEVICE.md` declares what the body *can* do; `SAFETY.md` declares
what it *must never* do, and which actions are governed by deterministic policy rather
than the language model.

**First principle: safety is below the brain.** Every rule here is enforced by the OS
(Go/Python policy), not by prompting the agent. The gateway may choose the wording of a
refusal; it may never be the thing that *decides whether* a safety-critical action
happens. This is not theoretical — guard mode in this codebase was already rebuilt to
deliver alerts deterministically because routing them through the agent was unreliable.
That is the template for everything here.

## motion

- **`motion.stop` is immediate, deterministic, and always available.** It does not queue
  behind the gateway, the network, or any in-flight skill. A spoken "stop" maps to a
  local intent that halts servos in the runtime, then informs the agent.
- Motion is **conservative by default** — bounded speed and acceleration, set in
  `os/hal/board/board.py`, not chosen by the agent.
- The agent **does not drive raw servo loops.** It requests poses and tracking targets;
  the runtime clamps to mechanical limits.
- No motion during a declared privacy-sensitive moment, during setup failure, or when
  the board reports a fault.
- Movement that could surprise a person is **explained** ("looking over here").

## light

- No sudden full-brightness output — ramp. The LED ring is calm by default.
- Respect quiet hours and a brightness ceiling independent of the agent.

## audio

- No loud output without reason; respect quiet hours.
- Never repeat secrets or overheard private speech back aloud.

## autonomy

- Proactive behavior is allowed within limits; **destructive actions are forbidden.**
- On network loss the device fails *quiet*, not chaotic: hold last safe state, keep
  local reflexes (stop, mute, sleep, wake) alive, do not improvise motion.

## fail-safe states

| Condition | Behavior |
|-----------|----------|
| Network loss | Local reflexes only; no new motion; hold pose |
| Gateway unreachable | System layer + local intents run; no agent-driven actuation |
| Board / driver fault | Disable the faulting capability, report health, keep the rest |
| Setup incomplete | Motion disabled; setup/identity reflexes only |
| Thermal / over-current | Halt motion, surface a health event |

Intern inherits this contract minus the `motion`/`light` sections it does not declare.
