# Reachy Mini

Reachy Mini is the third-party desk robot profile for Autonomous. It is the same
OS image and runtime contract as the other devices, with the body selected by
`devices/reachy-mini/DEVICE.md`.

## Files

| File | Purpose |
|------|---------|
| `recon.sh` | Read-only first-boot probe — runs all of `first-boot-plan.md` Phase 1 in one shot |
| `DEVICE.md` | Runtime contract: identity, board gate, gateway default, and declared capabilities |
| `SAFETY.md` | Deterministic safety bounds for motion and fail-safe behavior |
| `SOUL.md` | Reachy's default persona, adapted from Lamp but mapped to head/body/antenna expression |
| `spike-hal.sh` | HAL-only dev deploy: rsync HAL, install `.env` + ALSA aliases, borrow daemon media, run uvicorn in tmux |
| `spike-os.sh` | os-server-only dev deploy: cross-compile, seed `/root/config/config.json`, run the API in tmux |
| `spike-web.sh` | Web UI dev deploy: build the bundle, install nginx, write a spike vhost (`/api` → os-server, `/hw` → HAL, loopback-only) |
| `spike.sh` | Full dev deploy from a Mac: build, rsync, install deps, run HAL + os-server in tmux |
| `rootfs/opt/hal/.env` | Production HAL env, copied to `/opt/hal/.env` by the rootfs overlay |
| `rootfs/etc/asound.conf` | ALSA aliases (`device_mic`, `device_speaker`) for the single USB audio card |
| `docs/runtime.md` | English bring-up and runtime notes |
| `docs/recovery.md` | English recovery, SSH access, and WiFi impact notes |
| `docs/first-boot-plan.md` | English first-boot recon plan, setup.sh design, smoke tests |
| `docs/pollen-ecosystem-analysis.md` | Pollen ecosystem reference: voice, tool registry, app distribution |
| `docs/vi/runtime_vi.md` | Vietnamese bring-up and runtime notes |
| `docs/vi/recovery_vi.md` | Vietnamese recovery, SSH access, and WiFi impact notes |
| `docs/vi/first-boot-plan_vi.md` | Vietnamese first-boot recon plan |
| `docs/vi/pollen-ecosystem-analysis_vi.md` | Vietnamese Pollen ecosystem reference |

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
| `bash devices/reachy-mini/spike-hal.sh` | **First spike**: HAL only — validates body, audio, device profile |
| `bash devices/reachy-mini/spike-os.sh` | Second spike: os-server API only (loopback `:5000`, no web UI) |
| `bash devices/reachy-mini/spike-web.sh` | Third spike: nginx + web UI — makes the stack reachable from a browser |
| `REACHY_HOST=pollen@reachy-mini.local bash devices/reachy-mini/spike.sh` | Dev spike with os-server: build on Mac, rsync, tmux |
| `DEVICE_TYPE=reachy-mini install.sh` | Production: full setup.sh with systemd, nginx, OTA |

Run them in that order: `spike-hal.sh` (body) → `spike-os.sh` (API) →
`spike-web.sh` (browser). The web UI is not optional plumbing you can skip —
os-server binds `127.0.0.1:5000` and serves no static files, so nginx is what
makes both the bundle and the API reachable. None of the three is production:
Reachy's provisioning path — a NetworkManager-based `setup.sh` — is not written
yet.

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

Recon done on the first Wireless unit (2026-07-29) — results and consequences in
[docs/runtime.md](docs/runtime.md#first-boot-recon-measured-2026-07-29):

- daemon confirmed on `localhost:8000` (REST + `/ws/sdk`), `reachy_mini` 1.9.0
- audio device names resolved → `rootfs/etc/asound.conf` + `.env` filled in
- board gate taught the CM4 (`raspberry_pi_cm4`) — HAL refused to boot before this
- network stack is NetworkManager → setup.sh takes the `nmcli` branch
- **the daemon owns camera + audio**; HAL must `POST /api/media/release` first
- **camera is CSI/libcamera, not UVC** — the OpenCV index path does not work

Still needs a real-device spike:

- wire media release/acquire into HAL startup/shutdown
- pick and implement a camera path (picamera2 / daemon-mediated / rpicam subprocess)
- verify motion sign conventions and tune aim poses
- verify whether first-use recorded moves need network access to Hugging Face
- verify pygobject/pycairo build on Pollen's OS
- decide whether Reachy needs device-specific presets
