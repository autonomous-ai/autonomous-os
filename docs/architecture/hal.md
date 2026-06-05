# Hardware Abstraction Layer (HAL)

The HAL is the frozen interface between Autonomous and the hardware. Everything above it —
skills, the runtime, system services — speaks in **capabilities** and never knows which
servo, LED, or camera a device uses. This is what makes "a new device is a `DEVICE.md`,
not a fork" true.

## Capabilities vs. drivers

| | Capability | Driver |
|---|---|---|
| Example | `motion.move` | feetech servo on `/dev/ttyACM0` |
| Lives in | `contract/capabilities.md` | `os/hal/lelamp/service/*` |
| Stability | **frozen** — never renamed/removed in a major version | internal — changes freely |
| Addressed by | skills and the runtime | nothing above the HAL |

Two stability policies on purpose: capability names are an ABI third parties build on;
the drivers behind them churn. A skill says `motion.move`, never "feetech servo ID 3" —
that one rule lets a skill run on Lamp, Intern, or any third-party body.

## How a capability resolves

```
motion.move   capability   contract/capabilities.md      frozen
  └ route      os/hal/lelamp/routes/servo.py             HTTP surface
    └ driver   os/hal/lelamp/service/motors/*            talks to hardware
      └ board  platform/board.py                         which bus / pins
        └ node /dev/ttyACM0                              kernel device node
```

The same capability appears at each level with a different job: `DEVICE.md` declares it,
the route exposes it, the driver implements it, the board wires it.

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
declarations, no fork. *(planner: `os/hal/lelamp/platform/device.py`)*

## Adding a capability

Three existing artifacts, no new file type:

1. **Driver** under `os/hal/lelamp/service/<subsystem>/`, exposing a route.
2. **`contract/capabilities.md`** — add the name to the frozen vocabulary.
3. **`DEVICE.md`** — declare it on each device that has the hardware.

If it moves, heats, or emits light/sound, it must expose a deterministic stop governed by
[`SAFETY.md`](../../devices/lamp/SAFETY.md) — the stop never routes through the runtime.

## See also

[overview.md](overview.md) · [kernel.md](kernel.md) · [`DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md)
