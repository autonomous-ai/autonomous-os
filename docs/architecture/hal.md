# Hardware Abstraction Layer (HAL)

A **hardware abstraction layer (HAL)** is an interface with a standard, device-agnostic
contract that hardware implementations sit behind. It lets the upper layers of Autonomous
— the framework, the gateway, and skills — stay completely unaware of which servo, LED,
or camera a given device uses. They speak in **capabilities**; the HAL maps capabilities
to concrete drivers.

This is the layer that makes "a new device is a `DEVICE.md`, not a fork" true.

## Terms

| Term | Definition |
|------|------------|
| **Capability** | A named, device-agnostic ability — `audio.speak`, `motion.move`, `vision.snapshot`. The unit skills and agents address. Defined in [`contract/capabilities.md`](../../contract/capabilities.md). |
| **Route** | The concrete HTTP endpoint that implements a capability group on a device (`os/hal/lelamp/routes/*`). The typed surface; consumer is the system. |
| **Driver** | The code behind a route that talks to one piece of hardware (`os/hal/lelamp/service/*`). Internal, unstable. |
| **Capability contract** | The frozen, versioned set of capability names + their I/O shapes (`contract/`). The ABI third parties build against. |
| **Declaration** | A device's `DEVICE.md` lists which capability groups its body has. The HAL brings up only those. |

## The contract is frozen; drivers churn

Two stability policies, by audience — exactly Linux's split between the syscall ABI and
the in-kernel driver API:

- **Capability names + shapes are an ABI.** Once published, a capability name is never
  removed or repurposed within a major version. Skills and third-party devices build
  against it. Breaking changes bump the schema (`autonomous.device.v1` → `v2`).
- **Drivers are internal and unstable.** They live in-tree, so when a capability's
  implementation changes, every driver is fixed in the same commit. Out-of-tree drivers
  rot; in-tree drivers are carried along.

**Skills address capabilities, never hardware models.** A skill says `motion.move`, never
"feetech servo ID 3." That single rule is what lets one skill run on Lamp, Intern, Sphere,
or a third-party body.

## How a capability resolves

```
skill / agent              "motion.move"                 (capability — frozen)
        │
HAL route        os/hal/lelamp/routes/servo.py           (typed HTTP surface)
        │
driver           os/hal/lelamp/service/motors/*          (talks to hardware — unstable)
        │
platform         platform/board.py                       (which bus / pins on this board)
        │
Linux kernel     /dev/ttyACM0, spidev, gpiochip          (the actual device nodes)
```

The same capability shows up at multiple altitudes with different consumers — and that is
correct, not redundant. `DEVICE.md` *declares* it, the route *exposes* it, the driver
*implements* it, the platform *wires* it.

## Declaration-driven mounting

A device's `DEVICE.md` declares which capability groups its body has. At boot the runtime
brings up **only** the declared routes:

- **declared + driver present** → mount
- **declared + required + missing** → fail loud (a hardware fault)
- **declared + optional + missing** → skip (graceful degradation)
- **undeclared** → skip silently (a different device, by design)

This replaces an older implicit `try/except ImportError` skip that couldn't tell "no servo
by design" from "servo lib missing" from "servo broken." It is why **Intern is Lamp minus
`motion` + `display`** — the same image, fewer declarations, no fork. The planner lives in
`os/hal/lelamp/platform/device.py` (`plan_mounts`).

## Adding a new capability

No new file *type* — three existing artifacts:

1. **Driver** — implement it under `os/hal/lelamp/service/<subsystem>/` and expose a route.
2. **`contract/capabilities.md`** — add the capability name(s) to the frozen vocabulary.
3. **`DEVICE.md`** — declare the group on each device that has the hardware.

If a capability is safety-relevant (it moves, heats, or emits), it must expose an
immediate, deterministic **stop** and be governed by [`SAFETY.md`](../../devices/lamp/SAFETY.md) —
the stop never routes through the LLM.

## What's next

- [overview.md](overview.md) — the full stack
- [kernel.md](kernel.md) — the Linux device nodes the drivers ultimately use
- [`../../contract/DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md) — the `DEVICE.md` format + versioning
