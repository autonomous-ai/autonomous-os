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
thermal:
  # SoC over-temp → health event (/health) + stop discretionary tracking. 95°C is
  # provisional: this SoC idles hot (~80–90°C is normal), so verify the board's own
  # critical trip in /sys/class/thermal/.../trip_point_*_temp and tune. resume
  # defaults to max_temp_c - 10 (clears at 85°C).
  max_temp_c: 95
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
- On network loss the device fails *quiet*, not chaotic: stop agent-driven tracking,
  keep local idle presence + reflexes (stop, mute, sleep, wake) alive, and start no
  new agent-driven motion.

## fail-safe states

The behaviors below are the contract; the **Enforced** column states what the runtime
actually wires today (the rest are reserved — declared intent, not yet enforced, in the
spirit of "what isn't enforced isn't claimed as enforced").

| Condition | Behavior | Enforced |
|-----------|----------|----------|
| Network / gateway loss | Stop any in-flight object-tracking (don't chase a target with no fresh vision updates); local idle presence + reflexes (stop, mute, sleep, wake) stay alive; no new agent-driven motion | **yes** — `os/services` calls HAL `/servo/track/stop` on gateway WebSocket disconnect |
| Board / driver fault | Disable the faulting capability, keep the rest, report health | **yes** — per-capability `503` isolation in HAL routes + `/health` |
| Setup incomplete | Setup / identity reflexes only | reserved — not gated in the runtime yet |
| Thermal / over-current | Halt motion, surface a health event | reserved — **no thermal / current sensor on this hardware** |

Idle animation is local and self-contained, so the device stays "alive" (breathing,
emoting) when the cloud is gone rather than freezing — only *agent-driven* tracking and
new motion stop. `motion.stop`/release stay available throughout.

Intern inherits this contract minus the `motion`/`light` sections it does not declare.
