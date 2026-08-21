# Reachy Mini Runtime Notes

This is the device-specific runbook for `robots/reachy-mini`. It only documents
what differs from the shared Autonomous platform and Lamp reference behavior.

## References

Shared behavior is referenced, not copied:

| Topic | Reference |
|-------|-----------|
| `ROBOT.md` schema, capability mounting, `driver:` semantics | [`robots/contract/ROBOT-SPEC.md`](../../contract/ROBOT-SPEC.md) |
| Capability vocabulary | [`robots/contract/capabilities.md`](../../contract/capabilities.md) |
| HAL capability/route/driver layering | [`docs/architecture/hal.md`](../../../docs/architecture/hal.md) |
| Safety engine behavior | [`docs/safety.md`](../../../docs/safety.md) |
| Setup / AP mode / provisioning | [`docs/setup-flow.md`](../../../docs/setup-flow.md) |
| Lamp vision tracking implementation, still the reference for tracking internals | [`robots/lamp/docs/vision-tracking.md`](../../lamp/docs/vision-tracking.md) |

Hardware references checked on 2026-07-21:

- Pollen / Hugging Face Space: <https://huggingface.co/spaces/pollen-robotics/Reachy_Mini>
- Reachy Mini official site: <https://www.reachy-mini.org/>
- Seeed Studio hardware datasheet: <https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_hardware/>
- Claude Code project memory: `reachy-mini-port`

## What This Profile Declares

`ROBOT.md` declares this route surface:

| Capability | Routes | Required | Reachy-specific note |
|------------|--------|----------|----------------------|
| `audio` | `audio`, `speaker`, `voice` | yes | 4-mic array and 5 W speaker on the Wireless model |
| `vision` | `camera` | yes | Wide-angle head camera |
| `motion` | `servo` | yes | `driver: reachy_sdk`; Stewart-platform head, body yaw, antennas |
| `expression` | `emotion` | yes | Expression maps to movement/antenna posture/voice |
| `media` | `music` | yes | Playback shares the one USB card with TTS |
| `sensing` | `sensing` | no | Optional perception stack; same gating as other devices |
| `presence` | none | no | Behavior gate only |
| `lifelike` | none | no | Behavior gate only; routeless idle suite in the os-server |
| `companion` | `buddy` | no | `buddy` is an **os-server** route, so HAL has no driver for it |
| `system` | `system` | yes | Shared HAL system route |

The profile intentionally does **not** declare `light`, `display`, or `scene`.
Current Pollen/Hugging Face/Seeed references list motion, camera, mic
array, speaker, compute, IMU, Wi-Fi, battery, and animated antennas, but not a
device-addressable LED ring or screen. If a future hardware revision exposes
those, add the capability only with a matching HAL driver and safety behavior.

**Declared is not mounted.** HAL crosses the declarations with driver
availability at boot (`plan_mounts` in `hal/board/device.py`): declared +
available mounts, declared + *required* + missing fails loud, declared +
*optional* + missing is skipped. On the Wireless unit `GET /device` therefore
reports

```
routes:  [audio, camera, emotion, music, sensing, servo, speaker, system, voice]
skipped: [buddy]
```

— `companion` is declared but optional, and no HAL `buddy` driver exists, so the
route is skipped instead of aborting the boot.

## Deployment: Install On Top, Never Flash

Reachy Mini ships with **Pollen's OS** (Debian 13 trixie) on its onboard Pi. The
OS includes a daemon that owns the serial bus, runs the motor control loop,
computes inverse kinematics for the Stewart platform, and enforces hardware
safety clamps. **Flashing a golden image wipes the daemon and bricks the robot.**

Autonomous OS is always **installed on top** of Pollen's OS.

The spike scripts **run on the robot**. Copy the folder over, then run it there:

```bash
scp -r robots/reachy-mini pollen@reachy-mini.local:~/
ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
```

Nothing is built on a developer machine. Every artifact comes from the OTA
metadata feed (`https://cdn.autonomous.ai/os/ota/metadata.json` by default), the
same source `scripts/imager/build-orangepi.sh` and `scripts/provision/setup.sh`
read. The earlier version cross-compiled Go on a Mac and rsynced HAL out of a
working tree, which shipped whatever happened to be checked out there — not what
the fleet runs, so anything reproduced on the spike robot said nothing about
anyone else's build. Override the feed with `OTA_METADATA_URL=…`, or with
`metadata_url` in `/root/config/bootstrap.json`; `spike-lib.sh` prefers the file
so a robot pointed at a staging feed is not silently moved back to production.

OTA signing is opt-in. If `/root/config/bootstrap.json` contains a pinned
base64 Ed25519 `signing_public_key`, `install.sh`, the spike scripts, and
`software-update` verify the feed's `signed` envelope and each downloaded ZIP's
`sha256` before extraction. Without that field they retain the legacy metadata
and download flow, so already provisioned robots continue to update normally.
For a fresh verified install, pass the key with the one-liner, for example
`curl -fsSL …/install.sh | sudo env OTA_SIGNING_PUBLIC_KEY=… bash`; it is pinned
in `bootstrap.json` before any OTA component script runs.

Before replacing `os-server` or `bootstrap-server`, `software-update` retains
the previous binary at `/root/bootstrap/rollback/`. Use
`sudo software-update rollback os-server` (or `bootstrap`) to restore it; the
failed version is blocked until the feed publishes a different version.

`spike.sh` is a **thin orchestrator** — it reimplements nothing, it just runs the
component scripts in order. Each of those also runs standalone:

| Step | Script | What it installs |
|------|--------|------------------|
| 1 | `spike-device.sh` | `devices.reachy-mini` from OTA → `/opt/devices/reachy-mini`, then applies the package's `rootfs/` overlay onto `/` (that is where `/etc/asound.conf` and `/opt/hal/.env` come from) |
| 2 | `spike-hal.sh` | `hal` → `/opt/hal`, builds the venv (`uv sync --python 3.12 --extra hardware --extra reachy`), runs uvicorn on `127.0.0.1:5001` |
| 3 | `spike-os.sh` | `os-server` → `/usr/local/bin/os-server`, seeds `/root/config/config.json`, runs it **as root with `WorkingDirectory=/root`** |
| 4 | `spike-web.sh` | `web` → `/usr/share/nginx/html/setup`, installs nginx, writes the spike vhost |
| 5 | `spike-agent.sh` | Node.js 22 (NodeSource) + `openclaw` at the OTA-pinned version, seeds `/root/.openclaw`, runs `openclaw gateway run` on loopback `18789` |
| 6 | `spike-bootstrap.sh` | `bootstrap` → `/usr/local/bin/bootstrap-server`, seeds `/root/config/bootstrap.json`, installs `robots/reachy-mini/software-update` → `/usr/local/bin/software-update` (the helper the worker execs; without it every apply fails with `executable file not found in $PATH` while all units report healthy) |

Why that order, specifically:

- **device first** — HAL refuses to boot without `ROBOT.md`, and its `boards`
  list is what lets HAL accept a CM4 at all. Without `/etc/asound.conf` there is
  no ALSA default, and PortAudio then has no output device: every TTS call fails
  with "Error querying device -1" while `aplay` from a shell still works.
- **`WorkingDirectory=/root` for os-server** — `config.Load` reads the *relative*
  path `config/config.json`, so any other working directory silently points
  os-server and HAL at different config files.
- **the seeded `config.json` must name `openclaw_config_dir`** — a key absent
  from the file does *not* fall back to the `Default()` value in
  `system/server/config/config.go`; both `Load` and `ProvideConfig` unmarshal
  onto a zero-valued struct, so a missing key means `""`, not `/root/.openclaw`.
  os-server finds the gateway token at
  `filepath.Join(OpenclawConfigDir, "openclaw.json")`, which for an empty dir
  resolves to the *relative* `openclaw.json` → `/root/openclaw.json`. That file
  never exists, so the token is never read, the agent websocket reconnects every
  5s forever and `WaitForAgentReady` never returns — with no error in the log,
  since the join produced a perfectly valid path, just the wrong one. This only
  ever bit a fully clean install: `config.json` normally survives an uninstall.
- **web is not optional plumbing** — os-server binds `127.0.0.1:5000` and serves
  no static files, so nginx is what makes both the bundle and the API reachable.
  `/hw/` stays **loopback-only** (`allow 127.0.0.1; deny all`), matching the
  production vhost: the browser reaches hardware through os-server's authenticated
  `/api/hardware/*` proxy, never HAL directly.
- **bootstrap last** — it can restart os-server and HAL the moment it finds a
  newer build, and doing that while the rest of the stack is still installing
  turns a clean bring-up into a race.

One guard worth knowing before it fires: os-server calls `SwitchToAPMode()` at
startup whenever `set_up_completed` is false (`system/server/config_watch.go`).
Enabling it at boot on a provisioned robot would tear down the WiFi station and,
over SSH, lose the robot until someone attaches a keyboard. `spike-os.sh` refuses
to continue when `set_up_completed` is not true *and* `/usr/local/bin/device-ap-mode`
exists; when that script is absent the switch is a no-op, so it only warns.

Flags: `spike.sh [--no-deps] [--skip <step>] [--stop] [--uninstall]`. Every
component script takes `--uninstall`, and all but `spike-device.sh` also take
`--stop` (there is no running device profile to stop); on top of that,
`spike-device.sh --keep-env`, `spike-hal.sh --no-deps` and `spike-bootstrap.sh
--no-start`. `--no-deps` is passed only to the `hal` step — the others abort on
an unknown flag. Teardown keeps going past a failing step, so a half-installed
robot is still cleanable. `spike.sh` clears the metadata cache at the start of a
run and the six steps then share one snapshot of the feed, so a publish landing
mid-install cannot leave os-server and HAL on mismatched builds.

Everything runs under **systemd** (`hal`, `os-server`, `openclaw`, `bootstrap`)
and survives a reboot; tmux is gone. That is also why `--stop` and `--uninstall`
differ: `--stop` leaves the unit *enabled*, so the service comes back on the next
boot; `--uninstall` disables and removes it. Uninstall keeps state on purpose —
`/root/config/config.json` (provisioning), `/root/config/bootstrap.json` (the
feed URL every script reads), `/root/.openclaw` (the gateway token os-server has
cached), and `/etc/asound.conf` + `/opt/hal/.env` from the overlay.

What is not yet production is provisioning:
`DEVICE_TYPE=reachy-mini bash <(curl -fsSL .../install.sh)` would run `setup.sh`
with the WiFi AP and captive portal, and Reachy's NetworkManager branch of that
script is not written. The spike vhost lands in
`/etc/nginx/sites-available/reachy-spike`, not the production
`/etc/nginx/conf.d/<type>.conf`.

Two stacks run side by side on the same Pi:

| Layer | Owner | How |
|-------|-------|-----|
| Motion (head 6-DOF, body 360°, antennas) | Pollen daemon `:8000` | HAL calls SDK → daemon owns hardware I/O |
| Audio (mic, speaker, TTS) | HAL, **after** the daemon releases it | ALSA `plug:device_mic` / `plug:device_speaker`, `media_backend="no_media"` |
| Camera | HAL, **after** the daemon releases it | libcamera — **not** plain V4L2/OpenCV, see [Camera Stack](#camera-stack-libcamera-not-uvc) |
| Agent brain, setup, OTA | os-server + OpenClaw | Independent of Pollen |

The daemon holds the camera and both audio PCMs by default; "HAL owns media
directly" only becomes true after an explicit handover. See
[Media Ownership](#media-ownership-the-daemon-holds-camera-and-audio).

## First-Boot Recon (measured 2026-07-29)

Facts below were measured on the first Wireless unit (`hardware_id
e4a0ef5f04fafb94`) with [`../recon.sh`](../recon.sh) plus follow-up probes. They
replace the guesses this doc previously carried.

| Area | Measured |
|------|----------|
| Board | `Raspberry Pi Compute Module 4 Rev 1.1`, 3.7 GiB RAM, 46 °C idle |
| OS / kernel | Debian 13.3 (trixie), `6.12.62+rpt-rpi-v8`, aarch64 |
| Disk | 14 GB eMMC root, 7.7 GB used / 5.5 GB free (59 %) |
| Boot config | `/boot/firmware/config.txt` — `imx708` cam0+cam1, `uart3`, i2c fan (`emc2301`), IMU on i2c4 |
| Network stack | **NetworkManager** active (`wpa_supplicant` active, `dhcpcd` inactive) |
| NM profiles | `Glinks` (STA) + `Hotspot` (`mode=ap`, ssid `reachy-mini-ap`, `ipv4=shared`, `autoconnect=false`) |
| Pollen units | `reachy-mini-daemon.service` (AP launcher → daemon), `reachy-mini-bluetooth.service` (GATT), `gpio-shutdown-daemon.service` |
| Daemon | `reachy_mini` 1.9.0 in `/venvs/mini_daemon` (Python 3.12), runs as user `pollen` |
| Daemon ports | `:8000` REST+WS, `:8443`. Our `5001` (HAL), `5000` (os-server, loopback-only) and `80` are all free |
| Daemon WS paths | `/ws/sdk`, `/ws/daemon`, `/ws/full`, `/ws/raw`, `/ws/set_target`, `/ws/apps`, `/ws/logs`, `/ws/updates` |
| Control loop | ~**49 Hz** measured (`/api/daemon/status`), not the 100 Hz commonly quoted |
| Audio | one USB card: `card 0: Audio [Reachy Mini Audio], device 0` — capture **and** playback |
| Camera | CSI `imx708_wide` (4608×2592 10-bit RGGB) via unicam/libcamera; `rpicam-apps` + `gstreamer1.0-libcamera` present, `python3-picamera2` **not** installed |
| System Python | 3.13.5; `uv` **not** installed; `libcairo2-dev` + `libgirepository1.0-dev` + `pkg-config` already present |
| SSH | `pollen@reachy-mini.local` (password `root`); direct `root@` SSH is refused (`publickey,password`) |
| Recovery | `/restore/venvs/` present; BLE GATT service running (`bluetoothd`, name `reachy-mini`) |

Two consequences were not in the original port plan and are covered below: the
daemon owns media, and the camera is not a UVC device.

## Media Ownership: the daemon holds camera and audio

By default the Pollen daemon has `/dev/video0`, `/dev/video1`, the ISP nodes, and
**both** ALSA PCMs (`pcmC0D0c` capture, `pcmC0D0p` playback) open. A second
process cannot take them:

```bash
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 1 /tmp/t.wav
# arecord: main:850: audio open error: Device or resource busy
```

The daemon exposes a supported handover for exactly this case:

```bash
curl -s -X POST http://localhost:8000/api/media/release   # {"status":"ok"}
curl -s        http://localhost:8000/api/media/status     # {"available":false,"released":true,"no_media":false}
# ... HAL now owns mic, speaker, and camera ...
curl -s -X POST http://localhost:8000/api/media/acquire   # hand it back
```

Verified on the unit: after `release`, `arecord` records and `rpicam-jpeg`
captures a 1280×720 frame; after `acquire`, media status returns to
`{"available":true,"released":false}`. The daemon stays `active` and answers HTTP
throughout — releasing media does **not** disturb motion control.

**Wired into HAL, not into a script.** `ROBOT.md` declares `owner: pollen_daemon`
on the `audio` and `vision` capabilities; `hal/server.py` resolves each distinct
owner name through `hal/drivers/media_owner/factory.py` and calls `release()` as
Phase 0 of lifespan startup, `acquire()` on shutdown. Same selector shape as
`driver:` on motion and vision, so nothing in HAL learns which body is running.

It has to be inside the process because of ordering: the Reachy SDK happens to
release when a client connects, but that runs in HAL's motion-init thread and
races audio detection. Losing that race is silent and total — with the daemon
still holding the card, PortAudio cannot probe a single sample rate, the
configured ALSA output never enumerates, and TTS settles on output device -1 and
raises on every utterance while all the status endpoints still report healthy.
No spike script calls `/api/media/*` any more; the one exception is a best-effort
`acquire` in `spike-hal.sh --stop`, covering a HAL killed hard enough to skip its
own shutdown hook and leave the daemon deaf and blind.

Two details worth knowing: `release()` retries 5× at 2 s intervals, because the
daemon is a systemd service starting alongside HAL and may not be listening on a
cold boot; and it restores the persisted speaker level afterwards, because the
daemon's release handler resets the card's mixer to its own level (measured: 90 %
before, 62 % after).

This pairs with — and does not replace — the SDK's `media_backend="no_media"`,
which only stops the *SDK client* from grabbing media.

## Camera Stack: libcamera, not UVC

The head camera is a **CSI `imx708_wide`** (Camera Module 3 wide) behind
Raspberry Pi's unicam + libcamera pipeline. `/dev/video0` is the raw Bayer unicam
node, not a ready-to-read YUV stream:

| Probe | Result |
|-------|--------|
| `cv2.VideoCapture(0)` | `isOpened() == True`, `read() -> False` (`select() timeout`) |
| `cv2.getBuildInformation()` | `GStreamer: NO` — the wheel-built `opencv-python` cannot use a `libcamerasrc` pipeline |
| `rpicam-jpeg -o t.jpg --width 1280 --height 720` | works (216 KB JPEG) |
| `python3-picamera2` | not installed; apt candidate `0.3.33-1` |

**Resolved (2026-07-29)** by a second camera backend rather than a config value.
`ROBOT.md` selects it the same way motion picks its driver:

```yaml
vision:
  routes: [camera]
  driver: rpicam
  required: true
```

`hal/drivers/camera/factory.py` maps that to
`RpicamVideoCaptureDevice`, which runs `rpicam-vid --codec mjpeg -o -` as a child
process, splits the stream on JPEG markers and decodes the newest frame with
`cv2.imdecode`. Both backends satisfy `VideoCaptureDeviceBase`, so routes,
sensing and the tracker never learn which one is running. Lamp declares
`driver: opencv` for the UVC path.

Why not the alternatives:

- **`python3-picamera2`** — the apt package is built for the system interpreter
  (3.13 on trixie); HAL's venv runs 3.12, and libcamera's binary extension does
  not load across versions. Using it would mean moving all of HAL to 3.13.
- **Daemon-mediated camera** — `/api/media/release` hands over camera *and* audio
  together, so taking frames from the daemon would mean routing audio through it
  as well, replacing a path that already works.

Measured on the unit: 1280×720 MJPEG at 15 fps requested delivers ~14 fps for
~21% of one core, with the daemon's control loop running. The driver idles at 5
fps and switches to 15 only while a consumer is registered
(`acquire_consumer()`), which respawns the child because the rate is a launch
argument. `HAL_CAMERA_INDEX` is unused by this backend — libcamera selects the
sensor by pipeline — and `requires_v4l2_index = False` keeps boot from probing
`/dev/video*` for a node it will never open.

Known tuning gap: at the 5 fps idle rate libcamera may pick exposures long
enough to smear moving subjects. Raise the idle rate or cap `--shutter` when
sharp stills matter more than CPU.

## Motion Driver

Reachy selects the HAL motion backend through:

```yaml
motion:
  routes: [servo]
  driver: reachy_sdk
  required: true
  safety: SAFETY.md#motion
```

HAL resolves that to `hal/drivers/motors/reachy_service.py` through
`hal/drivers/motors/factory.py`. The driver implements the shared
`MotionService` contract, so `hal/routes/servo.py` stays hardware-neutral.

The driver is a thin client to Pollen's daemon:

```bash
REACHY_DAEMON_HOST=localhost
REACHY_DAEMON_PORT=8000
```

Reachy's HAL joint keys are degrees/mm, even though the SDK uses radians/meters:

| Joint key | Meaning |
|-----------|---------|
| `head_x.pos`, `head_y.pos`, `head_z.pos` | Head translation, mm |
| `head_roll.pos`, `head_pitch.pos`, `head_yaw.pos` | Head rotation, degrees |
| `body_yaw.pos` | Body rotation, degrees |
| `antenna_left.pos`, `antenna_right.pos` | Antenna angles, degrees |

Supported through shared `/servo` endpoints:

- pose/readiness: `/servo`, `/servo/position`, `/servo/status`
- movement: `/servo/move`, `/servo/aim`, `/servo/nudge`
- recovery/modes: `/servo/zero`, `/servo/hold`, `/servo/release`, `/servo/resume`
- expression moves: `/servo/play` when Reachy's recorded-move library is available

### Emotion → HF Move Mapping

HAL emotion names (CSV stems on Lamp) are mapped to Pollen's HF moves in
`_MOVE_MAP` (reachy\_service.py). The map uses preset constants from
`hal/presets.py` and targets moves from
[pollen-robotics/reachy-mini-emotions-library](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library)
(81 moves). Examples:

| HAL emotion | HF move |
|-------------|---------|
| `curious` | `curious1` |
| `happy_wiggle` | `cheerful1` |
| `sad` | `sad1` |
| `greeting` | `welcoming1` |
| `nod` | `yes1` |
| `headshake` | `no1` |
| `music_groove` | `dance1` |

Unmapped names are tried verbatim (callers can send HF names directly).
The 8 music styles are spread across Pollen's 3 dance moves. Tests guard full
preset coverage and validate all map values against the HF library.

`GET /servo` reports `available_recordings` in the same vocabulary as its
`current` field: a mapped move is listed under its HAL name (`music_groove`,
not `dance1`), the rest of the library verbatim. Listing raw HF names split the
two fields — the web monitor highlights the entry equal to `current`, so
nothing ever highlighted while a mapped move played.

### Music Groove Loop

`POST /audio/play` dispatches `music_start` (with the detected style) when
playback begins and `music_stop` when it ends — `hal/routes/music.py`. The
matching emotion applied on the same path is LED/display only
(`_apply_emotion_led_display`), so the servo side comes entirely from these two
events.

The driver handles both: `music_start` sets the groove and the play thread
repeats that move until `music_stop`, matching the Feetech backend
(`animation_service._continue_playback`). One dance move is a few seconds long,
so without the repeat the robot danced once and then sat still for the rest of
the track. An emotion played mid-track runs its one-shot and then hands the
servo back to the groove; `hold`, `zero`, `release`, and shutdown end it.
Everything else stays one-shot — a plain `/servo/play` never repeats.

### One Writer At A Time

A recorded move and a `goto_target`/`set_target` are two independent target
streams into the daemon — it accepts both, and the last writer wins each
control cycle, so an aim issued during an animation reads as the motors
fighting each other. Every direct-pose entry point (`move_to`, `send_positions`,
`aim`, `nudge`, `zero`, `hold`, `release`, `freeze`) therefore claims the servo
first: it invalidates the play thread and cancels the move in flight. One-shot
commands hand the servo back to the music groove after the move duration; the
tracker's `send_positions` keeps it. Two play threads cannot overlap either —
a pass holds a lock and re-checks ownership before it starts, so a thread stalled
in the (slow) first HF library load never streams on top of a newer one.

The feetech backend gets this for free: `aim` stops the animation event loop
before moving, and that loop is the only writer.

### Play Ramp

Pollen moves are absolute trajectories starting at their own frame 0, and
`play_move`'s `initial_goto_duration` defaults to `0.0` — the daemon snaps the
head there from wherever the previous move was interrupted. Every play now
passes a ramp instead (`HAL_REACHY_PLAY_RAMP_S`, default 0.5s — shorter than the
Feetech `HAL_SERVO_PLAY_RAMP_S` of 2.0s because a Pollen move is only ~3s and
the ramp is charged on top of it). When frame 0 is far from the current pose the
ramp is stretched by the SAFETY.md `motion.max_speed` gate, the same
`min_move_duration` aim/nudge use — which is why the driver takes the safety
policy at construction (`server.py`), having no route to carry it.

Frame 0 is read through `RecordedMove.evaluate(0.0)`, which returns the same
(head pose, antennas, body yaw) triple the joint conversion already speaks.

### Suppression, Hold And Freeze

Suppression is split the way the routes read it, not collapsed into one flag:

| Flag | Set by | Effect |
|------|--------|--------|
| `_released` | `/servo/release` | torque off — every play refused until `/servo/resume` |
| `_zero_mode` | `/servo/zero` | parked; `/servo/play` refused (`is_suppressed`) |
| `_hold_mode` | `/servo/hold`, scene presets | no ambient motion; `/servo/play` refused |
| `_hold_explicit` | `/servo/hold` only | `routes/emotion.py` also refuses scene-change emotions |
| `_frozen` | camera capture | no ambient motion while a consumer holds it |

`is_suppressed` = zero ∨ hold ∨ released, matching the Feetech backend. Emotions
are the emotion route's call: it reads `_hold_mode`/`_hold_explicit` and the
driver honours whatever it lets through. Both flags used to be missing here, so
the route saw no hold at all, dispatched anyway, and the driver dropped the
animation with only a debug line — the robot went quiet after a hold for no
visible reason.

`freeze()` never cancels the move in flight: vision snapshots are frequent, and
chopping a move for each one broke the animation being watched and restarted the
groove from frame 0 every few seconds. It stops the *next* pass instead, so the
head settles as soon as the current move ends and stays still for as long as the
freeze is held; `unfreeze()` restarts the groove only if the freeze actually
outlived a pass. The Feetech backend reaches the same place by pausing servo
writes and resuming mid-recording, which the daemon-side player cannot do.

Known deltas from Lamp:

- CSV upload is a Feetech/Lamp animation concept; Reachy's `add_recording` is a
  no-op until we decide whether uploaded moves matter.
- Idle/ambient motion is daemon-owned or recorded-move-library-owned, not the
  Feetech event loop — the music groove is the one client-side repeat.
- `/servo/track` is not production-ready for Reachy yet. The shared
  `tracker_service` still reaches into Lamp/Feetech internals and must be moved
  to `MotionService` accessors first.

## Safety Delta

Reachy's current `SAFETY.md` machine bounds:

```yaml
motion:
  max_speed: 60
  stop_always: true
```

The shared HAL safety layer stretches movement duration to respect `max_speed`.
`stop`, `zero`, `hold`, and `release` remain deterministic recovery actions.

Do not add a `thermal` block until the real Wireless unit's Raspberry Pi thermal
profile is measured.

## Bring-Up Checklist

1. Static profile check:

   ```bash
   python3 -m unittest devices.contract.cts.test_compatibility
   ```

2. **Copy the folder to the robot and run it there** — direct `root@` SSH is
   refused, so log in as `pollen` (password `root`) and `sudo`:

   ```bash
   scp -r robots/reachy-mini pollen@reachy-mini.local:~/
   ssh pollen@reachy-mini.local
   sudo bash ~/reachy-mini/spike.sh                  # everything
   sudo bash ~/reachy-mini/spike.sh --no-deps        # re-run without uv sync
   ```

   For the body alone, the first two steps are enough — and they must be run in
   that order, because HAL will not boot without the device profile:

   ```bash
   sudo bash ~/reachy-mini/spike-device.sh           # ROBOT.md, asound.conf, .env
   sudo bash ~/reachy-mini/spike-hal.sh              # HAL under systemd
   sudo bash ~/reachy-mini/spike-hal.sh --no-deps    # redeploy without uv sync
   sudo bash ~/reachy-mini/spike-hal.sh --stop       # stop + hand media back
   ```

   `spike-hal.sh` refuses to run the venv build with less than 4 GB free on `/`
   (the venv is ~2 GB — torch, opencv, polars, pyarrow — and uv's wheel cache can
   add as much again on a 14 GB eMMC that ships ~60 % full). It keeps that cache
   in `/opt/hal/.uv-cache` so `--uninstall` reclaims every byte it wrote.

3. **Manual install** (if not using the spike scripts):

   ```bash
   # System libs for pygobject/pycairo (reachy SDK transitive deps)
   apt install -y libcairo2-dev libgirepository1.0-dev pkg-config

   cd /opt/hal
   uv sync --python 3.12 --extra hardware --extra reachy
   ```

   Keep `reachy` separate from `hardware` in `pyproject.toml`. The Reachy SDK
   pulls pygobject/pycairo which build from source and need system libs absent
   on Lamp images. `pyproject.toml` declares `[[tool.uv.dependency-metadata]]`
   for both so that `uv lock` resolves without building their sdists.

4. Boot HAL with the Reachy profile. `DEVICE_TYPE` and `DEVICES_DIR` come from
   `/opt/hal/.env` when systemd starts it; set them by hand only outside the unit:

   ```bash
   DEVICE_TYPE=reachy-mini DEVICES_DIR=/opt/devices \
     .venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 \
     --timeout-graceful-shutdown 5
   ```

   Loopback, as the unit does: HAL is reached through os-server's
   `/api/hardware/*` proxy, never from the LAN. Without
   `--timeout-graceful-shutdown` uvicorn waits forever for open connections — an
   SSE or MJPEG stream holds SIGTERM until systemd's 90 s SIGKILL.

5. Confirm mounted routes:

   ```bash
   curl -s http://localhost:5001/device
   curl -s http://localhost:5001/health
   ```

   Expected when all required drivers are available: `audio`, `camera`,
   `emotion`, `music`, `sensing`, `servo`, `speaker`, `system`, `voice`, with
   `buddy` under `skipped` (declared optional, no HAL driver). `led` and
   `display` should be absent entirely — they are not declared.

6. Verify motion in safe order:

   ```bash
   curl -s http://localhost:5001/servo/position
   curl -s -X POST http://localhost:5001/servo/aim \
     -H 'content-type: application/json' \
     -d '{"direction":"center","duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/nudge \
     -H 'content-type: application/json' \
     -d '{"yaw":5,"pitch":0,"duration":1.0}'
   curl -s -X POST http://localhost:5001/servo/zero
   curl -s -X POST http://localhost:5001/servo/release
   ```

## Device .env and ALSA Config

The production `.env` lives at `robots/reachy-mini/rootfs/opt/hal/.env` (rootfs
overlay pattern — same as Lamp). It sets `DEVICE_TYPE=reachy-mini` and tuning
defaults. There is exactly one path onto the robot: the `devices.reachy-mini` OTA
package carries `rootfs/`, and whoever installs that package copies the subtree
onto `/` — `spike-device.sh` for a spike, the imager and `setup.sh` in production.
`spike-hal.sh` does not write either file; it *checks* for `/opt/hal/.env` and
dies pointing at `spike-device.sh` if it is missing.

`spike-device.sh` backs up any file it is about to overwrite to
`<path>.pre-autonomous` (once). Pollen ships no `/etc/asound.conf` today, so this
normally creates rather than replaces — but the backup exists so this script can
never be the reason their config vanished. `--keep-env` preserves an already-tuned
`/opt/hal/.env` while still installing the rest of the overlay; without it, the
package's `.env` wins. `--uninstall` removes `/opt/devices/reachy-mini` but
deliberately leaves `/etc/asound.conf` and `/opt/hal/.env` in place — other units
still read them.

Audio names are filled in from recon. Mic and speaker are the **same** USB card
(`card 0: Audio [Reachy Mini Audio], device 0`), so
`robots/reachy-mini/rootfs/etc/asound.conf` aliases both to it, addressed by
card **name** so the two HDMI cards cannot shift the index:

```
pcm.device_mic     { type plug; slave.pcm "hw:CARD=Audio,DEV=0" }
pcm.device_speaker { type plug; slave.pcm "hw:CARD=Audio,DEV=0" }
```

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker
```

That file deliberately does not set `pcm.!default` — the Pollen daemon shares the
same hardware and keeps whatever default it expects.

One behavioral delta from Lamp: because capture and playback are the same USB
device, they share a clock domain. Lamp's mic and speaker sit on separate USB
buses and drift, which is why barge-in is disabled there. Reachy has no such
drift, so acoustic echo cancellation is worth re-testing on this body rather than
inheriting Lamp's "barge-in off" default.

Camera: `HAL_CAMERA_INDEX` is inert on this body — see
[Camera Stack](#camera-stack-libcamera-not-uvc).

## Board Gate

The Wireless unit reports `Raspberry Pi Compute Module 4 Rev 1.1`, which contains
no `pi 4` substring — so before 2026-07-29 `assert_board_supported()` refused to
boot HAL here:

```
RuntimeError: Unknown board: device-tree model 'raspberry pi compute module 4 rev 1.1'
matches no entry in boards.json ... Refusing to boot on unidentified hardware
```

Fixed by adding a `raspberry_pi_cm4` entry (`match: ["compute module 4"]`) to
`hal/board/boards.json` and listing it in this device's `ROBOT.md` `boards`. Its
`led`/`button` wiring is inherited from `raspberry_pi_4` and is **unverified** —
Reachy declares neither `light` nor a GPIO button, so nothing reads it yet.
Verify before wiring either peripheral on a CM4.

## Hardware Spike TODOs

Resolved by the 2026-07-29 recon: ALSA names, camera hardware type, board id,
network stack, daemon port/API. Resolved since: the media handover is wired into
HAL startup/shutdown via `owner: pollen_daemon`, and the camera path is the
`rpicam` driver. Still open:

- sign convention for `head_yaw.pos`, `head_pitch.pos`, and antenna order
- whether `wake_up` / `goto_sleep` produce sound with `media_backend="no_media"`
- first-run behavior of `pollen-robotics/reachy-mini-emotions-library`
- verify emotion→HF move mapping looks/feels right on the robot
- re-test acoustic echo cancellation / barge-in (shared USB clock, unlike Lamp)
- thermal limits before enabling `SAFETY.md` `thermal`
- `uv` is not installed on Pollen OS; `spike-hal.sh` installs it to
  `/usr/local/bin` when absent, but production `setup.sh` must too
- the spike scripts install systemd units, nginx and the OTA bootstrap worker,
  but not provisioning: no WiFi AP, no captive portal, and the spike vhost is
  not the production one
- `setup.sh` must take the NetworkManager branch (`nmcli`), and can reuse the
  existing `Hotspot` profile (`reachy-mini-ap`, `ipv4=shared`) instead of
  installing hostapd/dnsmasq
