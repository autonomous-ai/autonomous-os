---
schema: autonomous.safety.v1

# Every machine-enforced safety knob, by capability group. Active lines are
# enforced; lines tagged (optional)/(reserved) are available but left unset — shown
# so this file reads as the complete picture. Authoritative field list:
# contract/SAFETY-SPEC.md. Fail-safe BEHAVIORS that aren't numeric bounds
# (network/gateway loss, board fault, setup) are not front-matter fields — they
# live in the "fail-safe states" table in the prose below.

light:
  max_brightness: 180        # 0–255 daytime ceiling; the LED route clamps any higher request
  # Quiet hours lower the ceiling on real wall-clock time (device runs all day; not a nightlight).
  quiet_hours: { start: "22:00", end: "07:00", max_brightness: 40 }   # 22:00–07:00 → ring dims to 40, agent-independent

#audio:
#  quiet_hours: { start: "22:00", end: "07:00" }   # suppress loud discretionary output (music) in-window; spoken replies still play

motion:
  max_speed: 120             # deg/s ceiling; the servo route stretches a move's duration so no joint exceeds it
  stop_always: true          # motion.stop/release are deterministic, never gated
  # max_accel: <int>         # (reserved) — no acceleration model yet

thermal:
  max_temp_c: 95             # SoC °C → health event (/health) + stop tracking. PROVISIONAL: this SoC idles hot (~80–90°C normal); verify the board's critical trip in /sys/class/thermal/.../trip_point_*_temp and tune
  # resume_temp_c: 85        # (optional) clears the over-state on cool-down; defaults to max_temp_c - 10
  # (over-current is a separate, reserved fail-safe — no servo current sensor wired; see the table below)
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
| Thermal (SoC over-temp) | At SoC temp ≥ `thermal.max_temp_c`: health event on `/health` + stop discretionary tracking; clears on cool-down to `resume_temp_c` (hysteresis). Idle stays alive | **yes** (when `thermal` declared) — background monitor reads `/sys/class/thermal` |
| Over-current (servo) | Halt motion, surface a health event | reserved — **no servo current sensor wired** |

Idle animation is local and self-contained, so the device stays "alive" (breathing,
emoting) when the cloud is gone rather than freezing — only *agent-driven* tracking and
new motion stop. `motion.stop`/release stay available throughout.

Intern inherits this contract minus the `motion`/`light` sections it does not declare.
