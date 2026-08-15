# Autonomous Compatibility Definition

What a device must satisfy to call itself **Autonomous-compatible**. This is the spec; the
enforcer is the test suite in [`cts/`](cts/). A device that passes the CTS may use the
Autonomous-compatible mark and run the skill catalog. This is the single thing that keeps a
hundreds-of-device ecosystem from fragmenting.

Keywords MUST / SHOULD / MAY per RFC 2119.

## MUST

A compliant device:

1. ships a [`DEVICE.md`](DEVICE-SPEC.md) with `schema: autonomous.device.v1`, a stable `id`,
   a `type`, and its `boards`;
2. declares the **`system`** capability (health, network, setup, OTA hooks);
3. declares **at least one primary sense or output** — `audio` or `vision`. The one
   exemption is `type: mock_body`: a body made of variables (`devices/sim`) has no
   sensors to declare. It is a test fixture, never a shippable device — it may not
   claim compatibility, appear in the installer, or be sold as an Autonomous device;
4. declares only capability groups defined in [`capabilities.md`](capabilities.md);
5. for every **declared `required` capability**, brings the driver up at boot or **fails loud**
   (no silent half-boot);
6. for any **safety-class capability** it declares (`motion`, `light`), ships a
   [`SAFETY.md`](../lamp/SAFETY.md) and exposes an **immediate, deterministic stop**
   that does **not** route through the agentic runtime;
7. returns the standard API envelope — `{"status":1,"data":…,"message":null}` on success,
   `{"status":0,"data":null,"message":…}` on failure;
8. supports **local setup** (provisioning without a cloud round-trip).

## SHOULD

9. support **OTA** update + rollback;
10. process sensing **locally first**, forwarding to the runtime only on cooldown;
11. ship a [`SOUL.md`](../lamp/SOUL.md) (a default character);
12. **degrade gracefully** — a skill that lists a capability as optional must run without it.

## MAY

13. include a camera, microphone, motors, LEDs, a display, biometric recognition, or a cloud
    agentic runtime — each declared as the matching capability, never assumed.

## MUST NOT

14. route a safety-critical stop, motion limit, or thermal cutoff through the LLM/runtime;
15. ship a `motion` capability without a deterministic e-stop;
16. mount a capability its `DEVICE.md` does not declare.

## Versioning

This document is versioned with the `contract` schema. A device targeting an older major
version remains compatible within that major; a new major (`autonomous.device.v2`) is a
separate compatibility generation, supported alongside the old for a deprecation window.
