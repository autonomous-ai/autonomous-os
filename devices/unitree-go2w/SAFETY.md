# SAFETY.md — Unitree Go2-W

The Go2-W moves through shared human space, so the safety floor matters more here than on any
desk device. Everything below is enforced by deterministic policy (the `safety` System
Manager), never by the agentic runtime.

## motion (locomotion)

- **E-stop is immediate** — it halts all drive instantly and does not queue behind the
  runtime, the network, or any in-flight skill.
- Speed and acceleration are **bounded**, and slower near detected people.
- **Obstacle stop**: the 3D depth camera halts motion before contact; the runtime cannot
  override it.
- No autonomous motion toward a person without explicit intent; no motion during a
  localization or sensor fault.
- Motion is **announced** before it happens ("moving to the kitchen").

## fail-safe states

| Condition | Behavior |
|-----------|----------|
| Network loss | Stop; hold position; local reflexes only |
| Depth / sensor fault | Stop motion; report health |
| Runtime unreachable | System Managers run; no agent-driven motion |
| Low battery / thermal | Return-to-dock or safe stop |
| E-stop pressed | All motion off until reset |
