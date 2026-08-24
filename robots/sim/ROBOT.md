---
schema: autonomous.device.v1
id: sim
name: Mock Body
type: mock_body
boards: [sim]
manufacturer: Autonomous
gateway: { default: openclaw }
capabilities:
  motion: { routes: [servo], driver: mock, required: true, safety: SAFETY.md#motion }
  system: { routes: [system], required: true }
soul_ref: SOUL.md
safety_ref: SAFETY.md
---

# Mock Body

A body made of variables. There is no hardware under this declaration: the
motion driver (`mock`, `hal/drivers/motors/mock_service.py`) keeps joints in a
dict, and the board entry `sim` exists so HAL has a wiring profile to load on a
machine with no device tree.

What the dict does model, because a simulator that got these wrong would teach
the wrong thing: moves interpolate over their commanded `duration` and block
until they arrive; `aim`/`nudge` obey the `motion.max_speed` ceiling from
`SAFETY.md`; recordings replay through the shared stretch-and-resample timing in
`hal/drivers/motors/recording_timing.py`, so an animation lasts what it lasts on
a body; and `release` drops the pitch joints to a gravity stop before cutting
torque, leaving yaw where it pointed. What it still does not model: inertia,
collision, real torque, and per-unit EEPROM servo calibration.

Run it on a laptop:

```bash
make sim DEVICE_TYPE=sim
curl -s -X POST localhost:5001/servo/aim -H 'content-type: application/json' -d '{"direction":"left"}'
curl -s localhost:5001/servo/position
```

`hal/test/test_sim_server.py` boots this exact ASGI server in CI and verifies
the declaration, mock driver, motion safety clamp, and `/servo/stop` over
HTTP. It is the no-robot path for skills that require `motion`; skills that
declare absent capabilities remain correctly unavailable on this minimal body.

On a laptop, HAL automatically falls back from the production `/var/log/hal`
directory to `/tmp/autonomous-hal` when it cannot create the former. Set
`HAL_LOG_DIR` explicitly to keep logs elsewhere; an explicit unwritable path
still fails loud.

`HAL_BOARD` is what makes it possible — without a `/proc/device-tree/model` no
board can be detected and HAL refuses to boot, which is the correct behavior on
real hardware. The override names a real `boards.json` entry, is refused if the
device does not declare it, and logs loudly.

## What this is for

- Running skills and `[HW:/servo/…]` markers end to end with no robot.
- Exercising the safety gate: `SAFETY.md` here is deliberately strict, so a
  clamp that fires on the mock body fires on a real one.
- The Reachy Mini Lite path — its daemon runs on your laptop too.

## What this is not

Not a simulator. Nothing models inertia, collision, or time; a move lands
instantly. Physics belongs to a real simulator (Pollen's MuJoCo Reachy is the
obvious first one), and that is still open work.

## Declared capabilities

`motion` and `system` only. Audio, vision and light are absent on purpose: a
mock speaker or camera would invite tests that pass here and fail on hardware.
Skills gated on those capabilities correctly do not install on this body.
