# Reachy Mini

Run Autonomous on a [Reachy Mini](https://huggingface.co/blog/reachy-mini) —
Pollen Robotics' desk robot. Autonomous gives it a voice pipeline, vision,
sensing, a web UI, an agent runtime and over-the-air updates, driving the body
through Pollen's own SDK.

<p align="center">
  <img src="images/reachy-icon.svg" alt="Reachy Mini" width="480"><br>
  <sub><code>images/reachy-icon.svg</code> is Pollen Robotics' artwork, used with attribution; not covered by this repo's license.</sub>
</p>

## Install

SSH into your robot and run:

```bash
curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/devices/reachy-mini/install.sh | sudo bash
```

That is the whole thing. No repo to clone, no Go or Node toolchain, no build
step, nothing copied from your laptop — the installer pulls every component from
the released OTA feed and puts each one behind a systemd unit, so the stack
comes back on its own after a reboot.

Takes ~10–15 minutes on a first run, most of it building HAL's Python
environment. When it finishes, open
`http://<robot-ip>/setup?debug=true&device_id=reachy-1` in a browser to complete
setup (`debug=true` shows the AI-key and chat steps the phone app would otherwise
fill in; `device_id` is any unique name).

**It installs alongside Pollen's software — it does not replace it.** Never
flash a golden image onto a Reachy Mini: that would wipe the Pollen daemon that
owns the hardware. The daemon keeps running and keeps driving the motors; HAL
borrows the camera and microphone from it and hands them back on shutdown.

Afterwards the scripts live on the robot, so you can undo it:

```bash
sudo bash /opt/devices/reachy-mini/spike.sh --stop        # stop everything
sudo bash /opt/devices/reachy-mini/spike.sh --uninstall   # remove it
```

### Requirements

- A wireless Reachy Mini — the one with the Pi inside (verified). The Lite has no onboard computer (its daemon runs on your laptop), so this path does not apply to it yet
  that is already set up and on your network
- SSH access to it — on the shipped Pollen OS that is the `pollen` user, which
  has passwordless sudo. Root cannot SSH in directly.
- ~4 GB free on the eMMC. The installer checks and refuses rather than wedging
  the robot, since a full disk hurts the Pollen daemon more than it hurts us.

### Options

```bash
# install from another feed (staging, a fork)
curl -fsSL …/install.sh | sudo OTA_METADATA_URL=https://…/metadata.json bash

# skip a step, e.g. bring the body up without the agent runtime
curl -fsSL …/install.sh | sudo bash -s -- --skip agent
```

## Set it up

One command adds a brain — talk, see, remember, learn skills, six swappable agents, OTA —
on top of Pollen's stack, not instead of it. Verified on the Wireless unit against Pollen's
daemon `reachy_mini` 1.9.0 (2026-07-29); the driver pins `reachy-mini>=1.9`.

1. **SSH in:** `ssh pollen@reachy-mini.local` — password from the SSH section of
   [Pollen's get-started guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/get_started).
   `pollen` has passwordless sudo, so the one-liner runs as-is.
2. **Install** — the one-liner above, 10–15 minutes. It refuses to run on anything that is
   not a Reachy Mini. ⚠️ Only this installer: never flash our SD-card image or run the Lamp
   installer on a Reachy, either takes over Pollen's OS
   ([reflash guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
   is the way back).
3. **Add it.** In the Autonomous app: **Add robot** → Reachy Mini, then type
   `reachy-mini.local` when it asks where the robot is. No app? Open
   `http://reachy-mini.local/setup?debug=true&device_id=reachy-1` — a debug flag, because
   the page was built for the app first; it shows the AI-key and chat-channel steps.
4. **Talk to it.** The head tilts, the antennas lift. Its 22 emotions play as moves from
   Pollen's own [emotion library](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library)
   — the whole expression layer here is a 28-line table in
   [`reachy_service.py`](../../hal/drivers/motors/reachy_service.py) — and any of the
   library's ~85 moves plays by name from a skill:
   `[HW:/servo/play:{"recording":"dance1"}]`.
5. **From here** it is the same as any body: give it a soul at
   `/opt/devices/reachy-mini/SOUL.md`, teach it skills, swap the brain — steps 3–5 of
   [Lamp's walkthrough](../lamp/README.md#set-it-up) apply as written. Web UI at
   `http://reachy-mini.local/monitor`. Undo any time:
   `sudo bash /opt/devices/reachy-mini/spike.sh --stop` / `--uninstall`.

Reachy Mini Lite (the daemon runs on your laptop) is not supported yet — it needs the
[mock body](https://github.com/autonomous-ai/autonomous-os/issues/200).

## What changes on your Reachy while it runs

- **Camera and mic are ours** — taken through the daemon's own `/api/media`
  handover, so Pollen apps that need them wait until you `--stop`.
- **Torque stays on** — HAL calls `enable_motors` + `wake_up` at every start,
  where a stock Wireless boots asleep and hand-posable: expect the wake move on
  every boot, a shorter battery day and warmer motors. Thermal bounds for
  Reachy are still open in `SAFETY.md`, so `--stop` it when you leave the desk
  for the day.
- **One head, one writer** — a Pollen app moving the head at the same time will
  fight it (the daemon takes targets from both, last writer wins each cycle).
  Run one or the other.
- **Sound clips are dropped** — HAL plays moves through the SDK's `no_media`
  client while it owns the speaker, so each move's `.ogg` sidecar is not played
  (a small wanted PR).
- **Expression layer** — the 22 emotions map onto Pollen's
  [emotion library](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library)
  through a 28-line table in `hal/drivers/motors/reachy_service.py`: 20 emotion
  moves (`curious1`, `welcoming1`, `yes1`…) plus 8 dance styles spread over
  Pollen's `dance1`–`dance3`. Any of the library's ~85 moves plays by name:
  `[HW:/servo/play:{"recording":"dance1"}]`.

`--stop` puts it back to sleep and hands everything straight back. What lands:
four systemd units, nginx, Node 22, one `/etc/asound.conf`, ~1.8 GB left on a
stock unit. Web UI: `http://reachy-mini.local/monitor`, login = last 4
characters of the Pi's serial (`grep Serial /proc/cpuinfo | tail -c 5`).

## Files

| File | Purpose |
|------|---------|
| `install.sh` | The one-command entry point above. Fetches the released device package from OTA and hands over to `spike.sh` — the only file pulled from git, deliberately small enough to read before piping it to a shell |
| `recon.sh` | Read-only first-boot probe — runs all of `first-boot-plan.md` Phase 1 in one shot |
| `ROBOT.md` | Runtime contract: identity, board gate, gateway default, and declared capabilities |
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
| Device contract | [`devices/contract/ROBOT-SPEC.md`](../contract/ROBOT-SPEC.md) |
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

This section is what `install.sh` does underneath, and how to drive the steps
individually while developing the port. For just installing, use the one-liner
at the top of this file.

The scripts **run on the robot**, not from a Mac. `install.sh` fetches them as
part of the OTA device package; to run a work-in-progress copy instead, put the
folder there yourself:

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

Each component script also takes `--stop` and `--uninstall`; `spike-hal.sh` takes
`--no-deps`, `spike-device.sh` takes `--keep-env`, `spike-bootstrap.sh` takes
`--no-start`.

What this is **not**: `scripts/provision/setup.sh`, the production provisioning
path used by the imager, which additionally sets up the WiFi AP fallback and the
captive-portal vhost. Reachy has no provisioning path of its own yet — it keeps
Pollen's NetworkManager setup and is configured over the LAN.

Order matters, and `spike.sh` enforces it. `spike-device.sh` runs first because
everything else reads what it installs — HAL refuses to boot without `ROBOT.md`,
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

Runs on the Wireless unit — recon 2026-07-29, media handover, `rpicam` camera and
emotion moves verified on hardware. Still open: the head-tracking port
(`/servo/track` speaks Lamp's joint names), aim sign conventions, thermal bounds.

Built before the first unit arrived:

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
- **the daemon owns camera + audio** → `ROBOT.md` declares `owner: pollen_daemon`
  on `audio` and `vision`, and HAL performs the handover itself at startup and
  shutdown (`hal/drivers/media_owner/`). No script calls `/api/media/*`.
- **camera is CSI/libcamera, not UVC** → `ROBOT.md` declares `driver: rpicam`,
  which reads MJPEG from an `rpicam-vid` child process instead of OpenCV

Still needs a real-device spike:

- verify motion sign conventions and tune aim poses
- verify whether first-use recorded moves need network access to Hugging Face
- verify pygobject/pycairo build on Pollen's OS
- decide whether Reachy needs device-specific presets
