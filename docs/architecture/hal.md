# Hardware Abstraction Layer (HAL)

The HAL is the frozen interface between Autonomous and the hardware. Everything above it —
skills, the runtime, system services — speaks in **capabilities** and never knows which
servo, LED, or camera a device uses. This is what makes "a new device is a `DEVICE.md`,
not a fork" true.

## Capabilities vs. drivers

| | Capability | Driver |
|---|---|---|
| Example | `motion.move` | feetech servo on `/dev/ttyACM0` |
| Lives in | `devices/contract/capabilities.md` | `hal/drivers/*` |
| Stability | **frozen** — never renamed/removed in a major version | internal — changes freely |
| Addressed by | skills and the runtime | nothing above the HAL |

Two stability policies on purpose: capability names are an ABI third parties build on;
the drivers behind them churn. A skill says `motion.move`, never "feetech servo ID 3" —
that one rule lets a skill run on Lamp, Intern, or any third-party body.

## How a capability resolves

```
motion.move   capability   devices/contract/capabilities.md      frozen
  └ route      hal/routes/servo.py             HTTP surface
    └ driver   hal/drivers/motors/*            talks to hardware
      └ board  hal/board/board.py                     which bus / pins
        └ node /dev/ttyACM0                              kernel device node
```

The same capability appears at each level with a different job: `DEVICE.md` declares it,
the route exposes it, the driver implements it, the board wires it.

For motion, the driver level is pluggable: `DEVICE.md`'s `driver:` field selects the
motion backend via a small factory (`hal/drivers/motors/factory.py`), and every backend
conforms to the `MotionService` protocol (`hal/drivers/motors/base.py`) — so the servo
routes never know which hardware is underneath.

## Declaration-driven mounting

A device's `DEVICE.md` declares which capabilities its body has. At boot the runtime mounts
only those:

| Case | Result |
|---|---|
| declared + present | mount |
| declared + **required** + missing | fail loud (hardware fault) |
| declared + optional + missing | skip (graceful) |
| undeclared | skip (a different device) |

This is why **Intern is Lamp minus `motion` and `display`** — same image, fewer
declarations, no fork. *(planner: `hal/board/device.py`)*

## Adding a capability

Three existing artifacts, no new file type:

1. **Driver** under `hal/drivers/<subsystem>/`, exposing a route.
2. **`devices/contract/capabilities.md`** — add the name to the frozen vocabulary.
3. **`DEVICE.md`** — declare it on each device that has the hardware.

If it moves, heats, or emits light/sound, it must expose a deterministic stop governed by
[`SAFETY.md`](../../devices/lamp/SAFETY.md) — the stop never routes through the runtime.

## See also

[overview.md](overview.md) · [kernel.md](kernel.md) · [`DEVICE-SPEC.md`](../../devices/contract/DEVICE-SPEC.md)
