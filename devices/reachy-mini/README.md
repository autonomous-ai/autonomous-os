# Reachy Mini

Reachy Mini is the third-party desk robot profile for Autonomous. It is the same
OS image and runtime contract as the other devices, with the body selected by
`devices/reachy-mini/DEVICE.md`.

## Files

| File | Purpose |
|------|---------|
| `DEVICE.md` | Runtime contract: identity, board gate, gateway default, and declared capabilities |
| `SAFETY.md` | Deterministic safety bounds for motion and fail-safe behavior |
| `SOUL.md` | Reachy's default persona, adapted from Lamp but mapped to head/body/antenna expression |
| `docs/runtime.md` | English bring-up and runtime notes |
| `docs/recovery.md` | English recovery, SSH access, and WiFi impact notes |
| `docs/first-boot-plan.md` | English first-boot recon plan, setup.sh design, smoke tests |
| `docs/vi/runtime_vi.md` | Vietnamese bring-up and runtime notes |
| `docs/vi/recovery_vi.md` | Vietnamese recovery, SSH access, and WiFi impact notes |
| `docs/vi/first-boot-plan_vi.md` | Vietnamese first-boot recon plan |

## Shared References

Reachy Mini docs are intentionally delta docs. Shared platform behavior lives in
the common references:

| Topic | Reference |
|-------|-----------|
| Device contract | [`devices/contract/DEVICE-SPEC.md`](../contract/DEVICE-SPEC.md) |
| Capability vocabulary | [`devices/contract/capabilities.md`](../contract/capabilities.md) |
| Compatibility rules | [`devices/contract/COMPATIBILITY.md`](../contract/COMPATIBILITY.md) |
| Safety engine | [`docs/safety.md`](../../docs/safety.md) |
| OS server / HAL API | [`docs/os-server.md`](../../docs/os-server.md) |
| Setup flow | [`docs/setup-flow.md`](../../docs/setup-flow.md) |
| Realtime voice / expression gating | [`docs/realtime-voice.md`](../../docs/realtime-voice.md) |

## Profile Summary

Reachy Mini declares audio, vision, motion, expression, sensing, presence, and
system capabilities. Motion is required and uses the `reachy_sdk` driver, which
wraps Pollen's Reachy Mini Python SDK through HAL's generic `MotionService`
contract.

It intentionally does **not** declare `light` or `display`. Current Pollen,
Hugging Face, and Seeed hardware references list head/body/antenna motion,
camera, microphone array, speaker, onboard compute, IMU, Wi-Fi, and battery for
the Wireless model, but not a device-addressable LED ring or screen. Reachy's
expression is therefore movement, antenna posture, gaze, and voice.

## Deployment

Reachy Mini ships with Pollen's OS on its Pi. **Never flash a golden image** —
it would wipe the Pollen daemon that owns the hardware. Autonomous is always
installed on top.

| Method | Use case |
|--------|----------|
| `REACHY_HOST=pi@<IP> bash devices/reachy-mini/spike.sh` | Dev spike: build on Mac, rsync, tmux |
| `DEVICE_TYPE=reachy-mini install.sh` | Production: full setup.sh with systemd, nginx, OTA |

See [docs/runtime.md](docs/runtime.md) for architecture details and bring-up checklist.

## Status

Code complete (pre-hardware):

- device declaration and safety profile
- Reachy persona
- motion driver selector (`reachy_sdk`) + factory
- draft Reachy motion driver with emotion→HF move mapping (28 entries)
- pyproject.toml `reachy` extra with dependency-metadata fix
- 12 static tests (factory, protocol conformance, move map coverage)
- dev deploy script (`spike.sh`)

Still needs a real-device spike:

- verify motion sign conventions and tune aim poses
- verify Pollen daemon startup and `REACHY_DAEMON_HOST` / `REACHY_DAEMON_PORT`
- verify camera and microphone device names on the shipped OS
- verify whether first-use recorded moves need network access to Hugging Face
- verify pygobject/pycairo build on Pollen's OS
- decide whether Reachy needs device-specific presets
