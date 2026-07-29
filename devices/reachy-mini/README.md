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
| `spike.sh` | Thin orchestrator: runs the six component scripts below in order (device → hal → os → web → agent → bootstrap) |
| `spike-lib.sh` | Shared library — sourced by every spike script: OTA metadata fetch, component install, systemd unit writing |
| `spike-device.sh` | Installs `devices.reachy-mini` from OTA into `/opt/devices` and applies its `rootfs/` overlay onto `/` |
| `spike-hal.sh` | Installs the `hal` component into `/opt/hal`, builds the venv, runs uvicorn under systemd |
| `spike-os.sh` | Installs the `os-server` binary into `/usr/local/bin`, seeds `/root/config/config.json`, runs it under systemd |
| `spike-web.sh` | Installs the `web` bundle into `/usr/share/nginx/html/setup` plus nginx and a spike vhost (`/api` → os-server, `/hw` → HAL, loopback-only) |
| `spike-agent.sh` | Installs Node.js 22 + the OTA-pinned `openclaw` version, seeds `/root/.openclaw`, runs the gateway under systemd |
| `spike-bootstrap.sh` | Installs the OTA bootstrap worker so the robot keeps itself current afterwards |
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

The spike scripts **run on the robot**, not from a Mac. Copy the folder over and
run it there:

```bash
scp -r devices/reachy-mini pollen@reachy-mini.local:~/
ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
```

Nothing is built on a developer machine. Every component — device profile, HAL,
os-server, web bundle, openclaw pin, bootstrap — comes from the OTA metadata feed
(`https://cdn.autonomous.ai/os/ota/metadata.json`), the same source
`scripts/imager/build-orangepi.sh` and `scripts/provision/setup.sh` read. That is
the point: a spike robot runs what the fleet runs, so a bug reproduced here says
something about everyone else's build. Point it elsewhere with
`OTA_METADATA_URL=…`, or with `metadata_url` in `/root/config/bootstrap.json`.

| Command (on the robot, as root) | Use case |
|--------|----------|
| `bash spike.sh` | Full bring-up: device → hal → os → web → agent → bootstrap |
| `bash spike.sh --no-deps` | Same, skipping HAL's `uv sync` (fast re-run) |
| `bash spike.sh --skip agent` | Skip one or more steps (repeatable) |
| `bash spike.sh --stop` / `--uninstall` | Tear down in reverse order |
| `bash spike-device.sh` | Device profile only — `/opt/devices`, `/etc/asound.conf`, `/opt/hal/.env` |
| `bash spike-hal.sh` | HAL only — validates body, audio, camera |
| `bash spike-os.sh` | os-server API only (loopback `:5000`, no web UI) |
| `bash spike-web.sh` | nginx + web UI — makes the stack reachable from a browser |
| `bash spike-agent.sh` | OpenClaw gateway only |
| `bash spike-bootstrap.sh` | OTA bootstrap worker only |
| `DEVICE_TYPE=reachy-mini install.sh` | Production: full setup.sh with systemd, nginx, WiFi AP, OTA |

Order matters, and `spike.sh` enforces it. `spike-device.sh` runs first because
everything else reads what it installs — HAL refuses to boot without `DEVICE.md`,
and without `/etc/asound.conf` there is no ALSA default, so every TTS call dies on
PortAudio device -1. `spike-bootstrap.sh` runs **last**: it can restart os-server
and HAL the moment it sees a newer build, and doing that mid-install turns a clean
bring-up into a race.

Every step that runs a service installs a systemd unit (`hal`, `os-server`,
`openclaw`, `bootstrap`) and survives a reboot — tmux is gone. Each script takes
`--uninstall`, and all but `spike-device.sh` take `--stop` (`--stop` leaves the
unit enabled, so it returns on the next boot). What is still not production is
provisioning: Reachy's
NetworkManager-based `setup.sh` branch is not written yet, and the spike nginx
vhost (`sites-available/reachy-spike`) is not the production one — no captive
portal, no `/gw` upgrade route.

See [docs/runtime.md](docs/runtime.md) for architecture details and bring-up checklist.

## Status

Code complete (pre-hardware):

- device declaration and safety profile
- Reachy persona
- motion driver selector (`reachy_sdk`) + factory
- draft Reachy motion driver with emotion→HF move mapping (28 entries)
- pyproject.toml `reachy` extra with dependency-metadata fix
- 12 static tests (factory, protocol conformance, move map coverage)
- OTA-driven spike scripts (`spike.sh` + six component scripts, all systemd)

Recon done on the first Wireless unit (2026-07-29) — results and consequences in
[docs/runtime.md](docs/runtime.md#first-boot-recon-measured-2026-07-29):

- daemon confirmed on `localhost:8000` (REST + `/ws/sdk`), `reachy_mini` 1.9.0
- audio device names resolved → `rootfs/etc/asound.conf` + `.env` filled in
- board gate taught the CM4 (`raspberry_pi_cm4`) — HAL refused to boot before this
- network stack is NetworkManager → setup.sh takes the `nmcli` branch
- **the daemon owns camera + audio** → `DEVICE.md` declares `owner: pollen_daemon`
  on `audio` and `vision`, and HAL performs the handover itself at startup and
  shutdown (`hal/drivers/media_owner/`). No script calls `/api/media/*`.
- **camera is CSI/libcamera, not UVC** → `DEVICE.md` declares `driver: rpicam`,
  which reads MJPEG from an `rpicam-vid` child process instead of OpenCV

Still needs a real-device spike:

- verify motion sign conventions and tune aim poses
- verify whether first-use recorded moves need network access to Hugging Face
- verify pygobject/pycairo build on Pollen's OS
- decide whether Reachy needs device-specific presets
