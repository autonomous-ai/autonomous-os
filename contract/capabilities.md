# Capability Vocabulary — `autonomous.capabilities.v1`

The frozen namespace of device capabilities — mostly physical hardware groups, plus a
few platform features a body opts into. A `DEVICE.md` declares which groups a body has;
a `SKILL.md` declares which it needs. Names here are an ABI — once published, a name is
never removed or repurposed (see `DEVICE-SPEC.md` § Versioning).

A capability group maps to one or more routes — HAL hardware routes under `os/hal/`, or
(for platform features like `companion`) os-server routes. The route is the typed HTTP
surface (consumer: the system); the capability is the agent-facing name (consumer: the
LLM). **Skills and agents address capabilities, never routes or hardware models.**

| Group | Capabilities | HAL routes (today) | Privacy | Safety |
|-------|--------------|--------------------|---------|--------|
| `audio` | `audio.speak`, `audio.listen` | audio, speaker, voice | microphone | loud-output |
| `vision` | `vision.snapshot`, `vision.stream` | camera, depth | camera | — |
| `sensing` | `sensing.presence`, `sensing.motion`, `sensing.sound`, `sensing.light` | sensing | ambient | — |
| `presence` | `presence.face`, `presence.emotion` | — (perception loop → sensing events; see below) | biometric | — |
| `motion` | `motion.move`, `motion.track`, `motion.stop` | servo, locomotion | — | motion |
| `light` | `light.paint`, `light.effect` | led, scene | — | bright-output |
| `display` | `display.render` | display | — | — |
| `expression` | `expression.emote` | emotion | — | — |
| `media` | `media.play` | music | — | loud-output |
| `connectivity` | `connectivity.bluetooth` | bluetooth | — | — |
| `companion` | `companion.control` | buddy (os-server) | computer | — |
| `system` | `system.health`, `system.ota`, `system.network`, `system.setup` | system | — | — |

## Perception vs expression — the two-way split

The OS is bidirectional, and every capability sits on one side of it. Classify it by
asking: does it take the world **IN**, or does it drive the body **OUT**?

- **Perception (IN).** `vision`, `sensing`, `presence` take the world *in*. `presence`
  (face identity, user emotion) is **routeless on purpose**: it runs a background loop —
  camera → dlbackend ML → `sensing` events POSTed up to the os-server — it is not a route
  the agent calls *down*. Declaring `presence` is what tells HAL to run that people-
  perception loop and which dlbackend models to call. (Raw ambient sensors are `sensing`;
  raw frames are `vision`. `presence` is the ML people-layer over them.) It also gates the
  idle→away→sleep auto-light state machine (dim → lights-off + `presence.away` sleep
  announcement): that machine's only `on_motion()` source is the people-perception loop, so
  a device without `presence` starts it disabled rather than falsely timing out to AWAY.
- **Expression / output (OUT).** Driven *down* from agent → os-server → HAL → body:
  - **`expression`** owns the `emotion` route — the body showing *its own* feeling. It is
    its own capability, not "lighting": a device declares `expression` when it has a way to
    emote (a screen face, an LED ring, or servo body-language), and the route **degrades**
    to whatever output is present. The realtime voice agent is also gated on it: its
    `express_emotion` tool is registered (and able to drive the `emotion` route from inside
    HAL) only when the device declares `expression` — a faceless device never sees the tool.
    See `docs/realtime-voice.md`.
  - **`light`** owns `led` (direct control) **and `scene`** — `scene` is an ambient *mode*
    (lighting + mic/speaker mute + camera/servo policy), not an emotion; it lives with
    `light` because the HAL route **requires an LED** (returns 503 without one) and degrades
    the rest. `display` and `motion` own their own routes.

> Historical note: `emotion` and `scene` were originally both mis-filed under `presence`,
> conflating expression (out) with perception (in). Corrected: `emotion` → its own
> `expression` capability; `scene` → `light` (it is a lighting mode, not an emotion);
> `presence` keeps its true meaning (people perception). Capability *names* already
> published are unchanged; `expression` is a new name added to the vocabulary.

## Rules

- **Address capabilities, not models.** A skill says `motion.move`, never "Feetech
  servo ID 3" — that lets the same skill run on any body that declares `motion`.
- **Degrade gracefully.** A skill that lists a capability as optional must run without
  it. A skill that requires a capability simply does not load on a device that lacks it.
- **`*.stop` is sacred.** Any group with a `safety` class must expose an immediate,
  deterministic stop. `motion.stop` never routes through the LLM (see `SAFETY.md`).
