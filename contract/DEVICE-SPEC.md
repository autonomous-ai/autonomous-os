# DEVICE.md Specification — `autonomous.device.v1`

`DEVICE.md` is the contract that describes a physical body to the Autonomous OS, the
agentic gateway, and skills. It is the device-side equivalent of `SKILL.md`: the YAML
front matter is the machine contract the runtime parses at boot; the prose below is
guidance the gateway and contributors read.

One file per device at `devices/<id>/DEVICE.md`. **Adding a device is writing a
`DEVICE.md`** (plus any missing drivers) — never a fork.

## How the OS consumes it

At boot the runtime reads the front matter and:

1. Brings up **only** the capability subsystems the device declares.
2. **Skips** undeclared capabilities silently — that is a different device, by design.
3. **Fails loudly** if a *declared* capability's driver is missing or won't initialize —
   that is a hardware fault, not a configuration choice.

This single rule is what turns "Intern" into "Lamp minus `motion` and `display`"
instead of a fork: the only difference between two devices is which capabilities they
declare. It replaces the old implicit `try/except ImportError` skip, which couldn't
tell "no servo by design" from "servo lib missing" from "servo broken."

## Front matter schema (v1)

| Field | Required | Meaning |
|-------|----------|---------|
| `schema` | yes | Contract version. `autonomous.device.v1`. |
| `id` | yes | Stable device id, e.g. `autonomous-lamp`. |
| `name` | yes | Display name. |
| `type` | yes | Free-form class (`desk_robot`, `desk_agent`). |
| `boards` | yes | Supported boards; resolved by `os/hal/platform`. |
| `gateway` | yes | Default agentic gateway + protocol. |
| `capabilities` | yes | Map of capability group → declaration (below). |
| `soul_ref` | no | Pointer to the character pack (closed layer — referenced, never embedded). |
| `safety_ref` | no | Path to this device's `SAFETY.md`. |
| `memory` | no | Memory backend declaration. |

### Capability declaration

Each entry under `capabilities` is a group from `contract/capabilities.md`:

```yaml
capabilities:
  motion:
    routes: [servo]           # HAL routes this group mounts
    driver: feetech           # implementation family (informational)
    required: false           # if true, a missing driver is a boot failure
    safety: SAFETY.md#motion  # bounds that govern this capability
```

`required: true` means "this device is not itself without this capability." Audio is
`required` on both Lamp and Intern; motion is `required` on neither.

## Versioning — the frozen contract

`schema` is an ABI. Within a major version fields are only **added**, never removed or
repurposed — a `v1` `DEVICE.md` must keep booting on every later `v1` runtime. Breaking
changes bump to `autonomous.device.v2`, and the runtime supports both across a
deprecation window. The capability vocabulary in `contract/capabilities.md` follows the
same rule: **names are forever.**

This is the Autonomous equivalent of the Linux syscall ABI / Android API level — the
contract facing devices and skills is stable; the drivers behind it churn freely.
