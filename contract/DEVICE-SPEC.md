# DEVICE.md Specification — `autonomous.device.v1`

`DEVICE.md` is the contract that describes a physical body to the Autonomous OS, the
agentic gateway, and skills. It is the device-side equivalent of `SKILL.md`: the YAML
front matter is the machine contract the runtime parses at boot; the prose below is
guidance the gateway and contributors read.

One file per device at `devices/<id>/DEVICE.md`. **Adding a device is writing a
`DEVICE.md`** (plus any missing drivers) — never a fork.

## How the OS consumes it

At boot the runtime reads the front matter and:

0. **Validates `schema`** — a missing, malformed, or unknown-major tag (e.g.
   `autonomous.device.v2` on a runtime that only understands `v1`) aborts boot.
   The runtime refuses to mount a body against an ABI it cannot read.
0b. **Verifies the `boards` gate** — resolves the physical board and aborts if it
   is unidentifiable or not in `boards`. Wrong board means wrong pin maps, a
   hardware fault, not a configuration choice.
0c. **Checks `id` against the folder** — `id` must equal the directory the
   profile is mounted from; a mismatch is a misplaced or mistyped profile and
   aborts boot. `id`/`name`/`type` are then exposed via HAL `GET /device`.
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
| `id` | yes | Stable device id. **Must equal the device folder name** (`devices/<id>/`); the runtime aborts boot on a mismatch. |
| `name` | yes | Display name. Exposed via HAL `GET /device`. |
| `type` | yes | Free-form class (`desk_robot`, `desk_agent`). Exposed via HAL `GET /device`. |
| `boards` | yes | Supported boards. At boot the runtime resolves the physical board (`os/hal/board`) and aborts if it is unknown or not in this list. |
| `gateway` | yes | Default agentic gateway (`default`) + wire transport (`protocol`). The transport follows from the runtime (openclaw→websocket, hermes→sse); `protocol` is validated for consistency against `default` (a warning, not a driver). |
| `capabilities` | yes | Map of capability group → declaration (below). |
| `soul_ref` | no | Soul artifact for this body: a path read relative to the device folder (e.g. `SOUL.md`), or an `http(s)://` URL the runtime downloads. Absent → the gateway's default soul. |
| `safety_ref` | no | The device's safety document: a path read relative to the device folder (e.g. `SAFETY.md`), or an `http(s)://` URL downloaded. Today it only feeds the per-capability anchor-consistency check (a warning); enforcement is a future engine. |
| `memory` | no | Memory backend declaration (`{ backend: <name> }`). Informational — the brain owns memory today; surfaced via HAL `GET /device`, not gated. |
| `startup_volume` | no | Speaker volume (0–100) os-server applies once at startup. Absent / out of range → `100` (software at max, so the hardware/alsactl level is the effective control). Lets a device with a loud speaker boot quieter instead of hardcoding the level. |
| `voice` | no | Voice defaults for this body (`{ tts_provider: <openai\|elevenlabs>, tts_voice: <name-or-id> }`). Both are **defaults** seeded into `config.json` once at startup, only when the user hasn't chosen them — so the Setup UI, HAL auto-start, and StartHALVoice all agree; the user's saved choice always wins. `tts_provider` absent / unknown → legacy default (`openai`). `tts_voice` pins the default voice explicitly (accepted verbatim). When the seeded provider is `elevenlabs` and `tts_voice` is absent, os-server picks a **language-aware** default (`vi`→Ngan, `zh`→Amy, else Rachel) so the voice matches the provider — an elevenlabs default with an openai voice like `nova` would otherwise 400. |

### Capability declaration

Each entry under `capabilities` is a group from `contract/capabilities.md`:

```yaml
capabilities:
  motion:
    routes: [servo]           # HAL routes this group mounts
    driver: feetech           # implementation family — informational, surfaced
                              # via GET /device; NOT gated (the route is the
                              # contract, the driver behind it churns freely)
    required: false           # if true, a missing driver is a boot failure
    safety: SAFETY.md#motion  # bounds that govern this capability
```

`required: true` means "this device is not itself without this capability." Audio is
`required` on both Lamp and Intern; motion is `required` on neither.

## Per-device presets (`presets.json`)

The emotion / scene / aim preset *values* and the LED ring size live in
`os/hal/presets.py` as the **platform default** every device inherits. A device
overrides only the values it wants different by shipping an optional
`devices/<id>/presets.json` — a sibling of `DEVICE.md`, discovered by convention
(no front-matter field declares it). At boot HAL (`board/presets_overlay.py`)
deep-merges it onto the base tables in place, before any route or driver reads
them; a device with no file keeps the defaults verbatim. This is the same
"declare what's different" inheritance as `devices/_base`, applied to look-and-feel.

```json
{
  "led_count": 60,
  "emotion":    { "listening": { "color": [255, 120, 0] } },
  "scene":      { "relax":     { "brightness": 0.3 } },
  "aim":        { "desk":      { "base_pitch.pos": 8.0 } },
  "status_led": { "booting":   { "color": [0, 60, 200] } }
}
```

- Every section (`led_count`, `emotion`, `scene`, `aim`, `status_led`) is optional.
  `status_led` restyles the os-server system-status feedback (booting/error/ota/
  connectivity/hal_down/agent_down/hardware/ready_flash + bootstrap OTA
  ota_progress/ota_error/ota_success + setup) — the OS owns the state machine,
  HAL owns the color/effect/speed. `setup` is a persistent solid; the rest are
  transient effect overlays.
- Each entry patches the matching base entry **field-by-field** — only the named
  fields change; the rest stay at the default.
- Naming a preset absent from the base table (a typo), a malformed file, or a
  non-positive `led_count` **fails loud at boot**, like an invalid `DEVICE.md`.
- **HAL-only:** presets are LED/servo look-and-feel; the OS core (Go) never reads
  them — unlike capabilities there is no second parser to keep in sync. An override
  only takes effect for routes the device mounts: `emotion` needs the `expression`
  capability, `scene` needs `light`, `aim` needs `motion`.
- Copy-paste reference: `devices/_base/presets.example.json` (annotated; the
  `.example.json` name is never loaded — rename to `presets.json` to activate).

## Versioning — the frozen contract

`schema` is an ABI. Within a major version fields are only **added**, never removed or
repurposed — a `v1` `DEVICE.md` must keep booting on every later `v1` runtime. Breaking
changes bump to `autonomous.device.v2`, and the runtime supports both across a
deprecation window. The capability vocabulary in `contract/capabilities.md` follows the
same rule: **names are forever.**

This is the Autonomous equivalent of the Linux syscall ABI / Android API level — the
contract facing devices and skills is stable; the drivers behind it churn freely.
