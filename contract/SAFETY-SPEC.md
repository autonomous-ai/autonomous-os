# SAFETY.md Specification — `autonomous.safety.v1`

`SAFETY.md` is the bounds contract for a device. `DEVICE.md` declares what a body
*can* do; `SAFETY.md` declares what it *must never* do, and which actions are
governed by deterministic OS policy rather than the language model.

Like `DEVICE.md`, it is two layers in one file:

- **YAML front matter** — the machine contract the **safety engine** parses at boot
  and enforces deterministically in the runtime (HAL).
- **Prose below** — the human-readable rationale the gateway and contributors read.
  The per-capability `safety: SAFETY.md#<anchor>` references in `DEVICE.md` point at
  these prose headings; the bounds that back them live in the front matter.

One file per device at `devices/<id>/SAFETY.md`, referenced by `DEVICE.md`'s
top-level `safety_ref`. It is **optional** — a device that declares no safety bounds
ships no `SAFETY.md`.

## First principle: safety is below the brain

Every bound here is enforced by the OS (Go/Python policy), not by prompting the
agent. The gateway may choose the *wording* of a refusal; it may never be the thing
that *decides whether* a safety-critical action happens. The agent cannot raise a
ceiling, skip a clamp, or disable a stop by asking — the runtime gate sits between
the agent's request and the hardware and is the single point that decides.

## How the OS consumes it

At boot the HAL runtime:

1. Resolves `safety_ref` from `DEVICE.md` to the `SAFETY.md` text (path or URL).
2. **Validates `schema`** — a missing/malformed/unknown-major tag aborts boot, like
   `DEVICE.md` (the runtime will not enforce a bounds ABI it cannot read).
3. Parses the front matter into a typed `SafetyPolicy`.
4. Exposes deterministic **gate** functions (e.g. `clamp_brightness`) that the
   capability routes call before actuating. The gate is pure, in-process, and runs
   on every request regardless of who issued it.

Bounds that are **declared** are enforced. Bounds that are **absent** are
unenforced (and logged) — see *Fail-safe* below for the per-capability rule.

## Front matter schema (v1)

```yaml
---
schema: autonomous.safety.v1
light:
  max_brightness: 180        # 0–255 ceiling; the runtime clamps any higher request
  quiet_hours: { start: "22:00", end: "07:00", max_brightness: 40 }  # reduced ceiling in-window
audio:
  quiet_hours: { start: "22:00", end: "07:00" }  # suppress loud output (music) in-window
motion:
  max_speed: 120             # deg/s; the servo route stretches a move's duration so no joint exceeds it
  stop_always: true          # motion.stop/release are deterministic and never gated
  # max_accel: <int>         # reserved
---
```

Times are device-local wall-clock (HH:MM, 24h); a window whose `start` > `end`
wraps past midnight (e.g. `22:00`→`07:00`). The gate reads the clock on every
request, so the bound changes with the time of day without a restart.

| Field | Required | Status | Meaning |
|-------|----------|--------|---------|
| `schema` | yes | enforced | Contract version. `autonomous.safety.v1`. Frozen ABI — fields are only added within a major. |
| `light.max_brightness` | no | **enforced (v1)** | Integer `0–255`. The LED route clamps any requested brightness to this ceiling. |
| `light.quiet_hours` | no | **enforced (v1)** | `{ start, end, max_brightness }`. Inside the window the LED ceiling drops to this lower `max_brightness`. (Slice 2.) |
| `audio.quiet_hours` | no | **enforced (v1)** | `{ start, end }`. Inside the window loud discretionary output (music via `/audio/play`) is suppressed; spoken replies still play. (Slice 2.) |
| `motion.max_speed` | no | **enforced (v1)** | deg/s ceiling. The servo route stretches a move's duration so no joint exceeds it (the move still reaches its target). (Slice 3.) |
| `motion.max_accel` | no | reserved | Acceleration ceiling. (Reserved — no accel model yet.) |
| `motion.stop_always` | no | **enforced (v1)** | `motion.stop`/release are deterministic and never gated, even when moves are fail-closed. (Slice 3.) |
| **(motion declared, no bounds)** | — | **fail-closed (v1)** | A device that declares the `motion` capability but ships no `motion:` bounds **refuses to actuate** (`/servo/move`, `play`, `aim`, `nudge`, `track`). The inverse of the light fail-safe. |

Sections are keyed by **capability group** (the same vocabulary as `DEVICE.md`
`capabilities` and `contract/capabilities.md`) so each `## <group>` prose heading,
its `DEVICE.md` `safety:` anchor, and its machine bounds line up.

## Fail-safe — what happens when a bound is absent or unloadable

The rule is **per-capability criticality**, not one global default:

- **Expressive capabilities (light, audio):** a missing bound is *pass-through* —
  the request is unclamped, and the runtime logs that no ceiling is set. A calm LED
  is not a safety risk, so the engine does not invent a limit nobody declared.
- **Actuating capabilities (motion):** a missing or unloadable bound is *fail-closed*
  — actuation is refused until bounds resolve. Moving a body against unknown limits
  is a hardware fault, not graceful degradation. (Enforced from slice 3.)

`SAFETY.md` itself is optional. A device with no `safety_ref` declares no bounds; the
gate is a no-op for it (and any `motion` it declares must carry its bounds inline, or
fail-closed once slice 3 lands).

## Versioning — the frozen contract

`schema` is an ABI, identical in discipline to `autonomous.device.v1`: within a major
version fields are only **added**, never removed or repurposed. A `v1` `SAFETY.md`
must keep enforcing on every later `v1` runtime. Breaking changes bump to
`autonomous.safety.v2`, supported across a deprecation window.

See `docs/safety.md` for the engine architecture and the slice roadmap.
