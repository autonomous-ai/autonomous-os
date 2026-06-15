# Safety Engine

The safety engine enforces a device's `SAFETY.md` bounds **deterministically in the
runtime**, below the agent. It is the mechanism behind the first principle in
`contract/SAFETY-SPEC.md`: *safety is below the brain.* The agent requests actions;
the engine decides — on every request, regardless of who issued it — whether the
hardware is allowed to honour them and within what limits.

> **Status:** Slice 1 (brightness ceiling) is **implemented and enforced** —
> `os/hal/safety/policy.py` + the LED gate in `rgb_service.py`, loaded from
> `devices/lamp/SAFETY.md`. Later slices reuse the same loader + gate. Each table
> row below is marked enforced / reserved. This document tracks the code, not the
> other way around.

## Why an engine, not prompting

Routing safety through the language model is unreliable — it can be argued out of a
refusal, it can hallucinate a limit, and it cannot guarantee an action *did not*
happen. Guard mode in this codebase was already rebuilt to deliver alerts
deterministically for exactly this reason. The safety engine generalises that: the
runtime, not the gateway, is the single point that clamps, blocks, or stops.

## Architecture

Three layers, mirroring the device layer (`DEVICE.md` → capability → route → driver):

```
SAFETY.md front matter        the declared bounds (machine contract; per capability group)
        │  resolved via DEVICE.md safety_ref (path or http), parsed at boot
        ▼
os/hal/safety/policy.py        pure SafetyPolicy + gate functions (no IO, unit-testable)
        │  clamp_brightness(requested) -> min(requested, ceiling)   [slice 1]
        ▼
HAL capability routes          call the gate BEFORE actuating (led, later servo/music)
        │  deterministic, in-process, cannot be bypassed by the agent
        ▼
hardware
```

- **`SAFETY.md` front matter** — the bounds, keyed by capability group. Schema and
  field table: `contract/SAFETY-SPEC.md`.
- **`os/hal/safety/policy.py`** — a pure loader (regex front-matter parse,
  dependency-free, same discipline as `os/hal/board/device.py`) producing a typed
  `SafetyPolicy`, plus pure gate functions. No hardware, no clock side effects, fully
  unit-testable off-hardware.
- **The routes** — each capability route consults the gate at the point of actuation.
  The LED route clamps brightness; later the servo route clamps speed/accel and
  guarantees `stop`. Because the gate is a plain function call in the request path,
  there is no path around it.

The policy is loaded once at boot (alongside the `DeviceProfile`) and exposed via the
device endpoint so the declared bounds are observable: `GET /device` already returns
`safety_ref`; the resolved bounds surface there too.

## Fail-safe semantics

Per-capability criticality (full rule in `contract/SAFETY-SPEC.md`):

| Capability | Bound absent / unloadable | Rationale |
|------------|---------------------------|-----------|
| light, audio | pass-through (log only) | a calm light/quiet speaker is not a hazard; do not invent a limit |
| motion | **fail-closed** (refuse actuation) | moving against unknown limits is a hardware fault |

`SAFETY.md` is optional. The schema tag is validated like `DEVICE.md`'s — a missing or
unknown-major `schema` aborts boot rather than enforce an ABI it cannot read.

## Slice roadmap

| Slice | Scope | Gate | Enforced where | Status |
|-------|-------|------|----------------|--------|
| 1 | `light.max_brightness` ceiling | `clamp_brightness` / `clamp_color` | LED gate (`rgb_service` `_handle_solid`/`_handle_paint`) | **enforced (v1)** |
| 2 | `quiet_hours` (light + audio) | `active_max_brightness` (time-aware) + `audio_quiet_now` | LED gate + music route | **enforced (v1)** |
| 3 | `motion.max_speed` + `stop_always`, **fail-closed** | `motion_allowed` + `min_move_duration` | servo route | **enforced (v1)** (`max_accel` reserved) |
| 4 | fail-safe states (network loss → hold pose, board fault → disable capability) | state gate | lifespan + routes | reserved |

Each slice adds fields to the `SafetyPolicy` and gate functions and wires one or more
routes; the loader and the front-matter contract do not change shape between slices
(fields are only added — the `autonomous.safety.v1` ABI).

## Verifying enforcement

A safety bound is only real if you can *prove* it holds and that the agent cannot get
around it. Each slice is verified at three levels; a bound is not "done" until all
three pass. (This is distinct from `devices/lamp/docs/security-test.md`, which covers
network/access-control security — ports, RCE, CORS — not actuation bounds.)

1. **Unit (pure gate, off-hardware).** The gate function is pure, so its limit is a
   table test: a request above the ceiling clamps to it, a request below passes
   through unchanged, an absent bound behaves per the fail-safe rule. Runs in CI with
   no device.
2. **Runtime (on the device, through the real route).** Issue the actuation request
   over HTTP and observe the hardware-bound value, not the requested one. The declared
   bound is also observable at `GET /device`, so the test asserts *request vs. ceiling
   vs. observed output* line up.
3. **Bypass audit (the safety-critical check).** Confirm there is **no** path to the
   actuator that skips the gate — issue the same action through every route that can
   drive it (agent path, direct route, any raw/low-level endpoint) and confirm each is
   clamped. A bound enforced on one path but reachable on another is not enforced.

### Slice 1 — brightness ceiling (checklist)

- [x] **Unit:** `clamp_brightness(255)` with `max_brightness: 180` → `180`;
      `clamp_brightness(120)` → `120`; no `max_brightness` → unchanged (pass-through).
      `clamp_color` scales hue-preserving (white→180,180,180; red→180,0,0). Schema
      missing/malformed/unknown-major fail-loud. (`os/hal/test/test_safety.py`, 21 tests.)
- [x] **Runtime:** verified on a real Lamp — `GET /device` returns
      `"safety": {"light": {"max_brightness": 180}}`; `POST /led/solid` at full white
      (255) reads back `[180,180,180]`, `[100,50,0]` passes unchanged, `[255,0,0]` →
      `[180,0,0]`.
- [x] **Bypass audit:** the gate sits in `rgb_service` `_handle_solid` / `_handle_paint`
      — the single chokepoint every pixel write funnels through. All callers (LED
      routes, effects, app_state, scene, gpio_button, presence, smooth_animation, main)
      use `dispatch(RGB_CMD_*)` → those handlers; the only direct `_driver` writes are
      inside them (post-clamp) and `clear()` (black). Grep confirmed no path skips it.
- [x] **Determinism:** `clamp_color` is pure and never consults the caller, so the
      clamp is identical for the agent, the Web UI, or a raw `curl`.

### Slice 2 — quiet hours (checklist)

Quiet hours add a **time** dimension: the LED ceiling drops and music is
suppressed inside a daily window (device-local wall-clock; the device runs all
day, so this is real time-of-day, not "off at night"). The gate reads the clock
on every request, so it flips at the boundary with no restart.

- [x] **Unit (injected clock):** `in_window` handles the midnight wrap
      (22:00→07:00 true at 23:00 and 06:00, false at 12:00, end-exclusive at 07:00);
      `active_max_brightness` returns the reduced ceiling (40) inside the window and
      the base (180) outside; `clamp_color((255,255,255), now=23:00)` → `(40,40,40)`,
      `now=12:00` → `(180,180,180)`; `audio_quiet_now` true in-window, false out /
      when no policy. (`os/hal/test/test_safety.py`.)
- [x] **Runtime:** `GET /device` reports `safety.light.quiet_hours` +
      `safety.audio.quiet_hours`. With the window set to the current time, the LED
      ring clamps to the reduced ceiling and `POST /audio/play` returns
      `{"status":"suppressed"}`; outside the window both behave normally.
- [x] **Bypass audit:** the LED quiet ceiling rides the same `rgb_service`
      `_handle_solid`/`_handle_paint` chokepoint as slice 1 (no new path). Music
      suppression sits in `/audio/play` — the only route that starts discretionary
      audio (TTS is intentionally exempt).
- [x] **Determinism:** the window check is pure given `now`; the only impurity is
      reading the system clock, isolated in `policy._now()` so tests inject time.

### Slice 3 — motion (checklist)

Motion is **fail-closed** (the inverse of light): a device that declares the
`motion` capability but ships no `motion:` bounds refuses to actuate. `max_speed`
is enforced by *stretching a move's duration* (the move still reaches its target;
only speed is capped) — never by truncating the destination. Recovery actions
(`release`/`zero`/`hold`/`stop`) stay ungated so you can always safe the body.

- [x] **Unit:** `motion_allowed` → False when motion declared but no bounds (or no
      policy), True with bounds, True for non-motion devices; `min_move_duration`
      stretches a too-fast move (120° at 120 deg/s → 1.0s), passes a slow one,
      bounds an instant (duration 0) request, passes through with no `max_speed`,
      ignores joints with no known start; commented `# stop_always` is not parsed
      as a bound; `max_speed: 0` fails loud. (`os/hal/test/test_safety.py`.)
- [ ] **Runtime (NOT yet verified on device — user deploys):** `GET /device`
      reports `safety.motion`; `/servo/move` at a tiny duration returns a stretched
      `duration` and the ring/arm moves at the capped speed; a device with motion
      declared but no bounds returns 403 on `/servo/move|play|aim|nudge|track`.
- [x] **Bypass audit (routes):** the fail-closed guard `_require_motion_bounds()`
      is on every discretionary servo endpoint (move, play, aim, nudge, track,
      track/update); recovery endpoints are intentionally exempt. NOTE: internal
      animation (idle/emotion poses driven by the runtime, not the agent) is not
      gated — that is device-controlled, not agent-requested; revisit if needed.
- [x] **Determinism:** `motion_allowed`/`min_move_duration` are pure (no clock, no
      caller identity).

## Relationship to existing ad-hoc enforcement

Some safety behaviour already exists, hardcoded and scattered: `motion.stop()` in the
motors/animation services, lerobot's mechanical position clamp, the LED brightness
scaling config. The engine does not rip these out at once — it *centralises* them
into the declared policy one slice at a time, so the bounds become data a device
declares rather than constants buried in drivers. Slice 1 introduces the engine with
a bound that has **no** prior enforcement (an agent-independent brightness ceiling),
proving the path end to end before migrating the existing pieces.
