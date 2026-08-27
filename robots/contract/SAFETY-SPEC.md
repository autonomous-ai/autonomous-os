# SAFETY.md Specification — `autonomous.safety.v1`

`SAFETY.md` is the bounds contract for a device. `ROBOT.md` declares what a body
*can* do; `SAFETY.md` declares what it *must never* do, and which actions are
governed by deterministic OS policy rather than the language model.

Like `ROBOT.md`, it is two layers in one file:

- **YAML front matter** — the machine contract the **safety engine** parses at boot
  and enforces deterministically in the runtime (HAL).
- **Prose below** — the human-readable rationale the gateway and contributors read.
  The per-capability `safety: SAFETY.md#<anchor>` references in `ROBOT.md` point at
  these prose headings; the bounds that back them live in the front matter.

One file per device at `robots/<id>/SAFETY.md`, referenced by `ROBOT.md`'s
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

1. Resolves `safety_ref` from `ROBOT.md` to the `SAFETY.md` text (path or URL).
2. **Validates `schema`** — a missing/malformed/unknown-major tag aborts boot, like
   `ROBOT.md` (the runtime will not enforce a bounds ABI it cannot read).
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
  max_volume: 40             # 0–100 % speaker ceiling; the volume route clamps any higher request
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
| `audio.max_volume` | no | **enforced (v1)** | Integer `0–100` (%). `POST /audio/volume` clamps any higher request, above the speaker/Bluetooth-sink split, so the ceiling binds every caller alike — agent, local intent, web slider, boot restore. All-day and independent of `audio.quiet_hours`. Both `GET` and `POST /audio/volume` report `max_volume`, and the `POST` reply carries the volume actually applied, so a UI can bound its own control instead of snapping back. (Slice 5.) |
| `audio.quiet_hours` | no | **enforced (v1)** | `{ start, end }`. Inside the window loud discretionary output (music via `/audio/play`) is suppressed; spoken replies still play. (Slice 2.) |
| `motion.max_speed` | no | **enforced (v1)** | deg/s ceiling. The servo route stretches a move's duration so no joint exceeds it (the move still reaches its target). (Slice 3.) |
| `motion.max_accel` | no | reserved | Acceleration ceiling. (Reserved — no accel model yet.) |
| `motion.stop_always` | no | declared (v1) | `motion.stop`/release/zero/hold are currently deterministic recovery actions because no route gates them. The field is parsed and reported, but no HAL route consumes it as an independent enforcement gate. |
| **(motion declared, no bounds)** | — | **pass-through (v1)** | Presence-driven, like light/audio: a device that ships no `motion:` bounds moves unrestricted (that is the *off* state, not a refusal). A declared `max_speed` is enforced; an absent one is not. |
| `thermal.max_temp_c` | no | **enforced (v1)** | SoC °C ceiling. A background monitor reads `/sys/class/thermal`; at/above this it raises a health event (`/health`) and stops discretionary motion (tracking), clearing on cool-down. Threshold is SoC-specific — read the board's own critical trip, not a generic guess. (Slice 4.) |
| `thermal.resume_temp_c` | no | **enforced (v1)** | Cooled to/below this clears the over state (hysteresis). Defaults to `max_temp_c − 10`. (Slice 4.) |

Sections are keyed by **capability group** (the same vocabulary as `ROBOT.md`
`capabilities` and `robots/contract/capabilities.md`) so each `## <group>` prose heading,
its `ROBOT.md` `safety:` anchor, and its machine bounds line up.

## Fail-safe — what happens when a bound is absent or unloadable

The rule is **presence-driven and uniform** across every capability: a declared bound
is enforced, an absent one is *pass-through* — the request is unclamped and the runtime
logs that no ceiling is set. The engine never invents a limit nobody declared, whether
the capability is a LED or a servo. Removing a section (or the whole front matter) is
how you turn that enforcement off; there is no separate kill switch.

A *malformed* bound (present but out of range, e.g. `max_speed: 0`) still fails loud —
only *absence* is pass-through.

`SAFETY.md` itself is optional. A device with no `safety_ref` declares no bounds; the
gate is a no-op for it. A device that declares `motion` but ships no `motion:` bounds
moves unrestricted — by design, so bring-up and unbounded hardware need no special flag.

## Versioning — the frozen contract

`schema` is an ABI, identical in discipline to `autonomous.device.v1`: within a major
version fields are only **added**, never removed or repurposed. A `v1` `SAFETY.md`
must keep enforcing on every later `v1` runtime. Breaking changes bump to
`autonomous.safety.v2`, supported across a deprecation window.

See `docs/safety.md` for the engine architecture and the slice roadmap.
