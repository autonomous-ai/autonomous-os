# Reachy Mini Runtime Notes

This is the device-specific runbook for `devices/reachy-mini`. It only documents
what differs from the shared Autonomous platform and Lamp reference behavior.

## References

Shared behavior is referenced, not copied:

| Topic | Reference |
|-------|-----------|
| `DEVICE.md` schema, capability mounting, `driver:` semantics | [`devices/contract/DEVICE-SPEC.md`](../../contract/DEVICE-SPEC.md) |
| Capability vocabulary | [`devices/contract/capabilities.md`](../../contract/capabilities.md) |
| HAL capability/route/driver layering | [`docs/architecture/hal.md`](../../../docs/architecture/hal.md) |
| Safety engine behavior | [`docs/safety.md`](../../../docs/safety.md) |
| Setup / AP mode / provisioning | [`docs/setup-flow.md`](../../../docs/setup-flow.md) |
| Lamp vision tracking implementation, still the reference for tracking internals | [`devices/lamp/docs/vision-tracking.md`](../../lamp/docs/vision-tracking.md) |

Hardware references checked on 2026-07-21:

- Pollen / Hugging Face Space: <https://huggingface.co/spaces/pollen-robotics/Reachy_Mini>
- Reachy Mini official site: <https://www.reachy-mini.org/>
- Seeed Studio hardware datasheet: <https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_hardware/>
- Claude Code project memory: `reachy-mini-port`

## What This Profile Declares

`DEVICE.md` declares this route surface:

| Capability | Routes | Required | Reachy-specific note |
|------------|--------|----------|----------------------|
| `audio` | `audio`, `speaker`, `voice` | yes | 4-mic array and 5 W speaker on the Wireless model |
| `vision` | `camera` | yes | Wide-angle head camera |
| `motion` | `servo` | yes | `driver: reachy_sdk`; Stewart-platform head, body yaw, antennas |
| `expression` | `emotion` | yes | Expression maps to movement/antenna posture/voice |
| `sensing` | `sensing` | no | Optional perception stack; same gating as other devices |
| `presence` | none | no | Behavior gate only |
| `system` | `system` | yes | Shared HAL system route |

The profile intentionally does **not** declare `light`, `display`, `scene`, or
`music`. Current Pollen/Hugging Face/Seeed references list motion, camera, mic
array, speaker, compute, IMU, Wi-Fi, battery, and animated antennas, but not a
device-addressable LED ring or screen. If a future hardware revision exposes
those, add the capability only with a matching HAL driver and safety behavior.

## Deployment: Install On Top, Never Flash

Reachy Mini ships with **Pollen's OS** (Debian 13 trixie) on its onboard Pi. The
OS includes a daemon that owns the serial bus, runs the motor control loop,
computes inverse kinematics for the Stewart platform, and enforces hardware
safety clamps. **Flashing a golden image wipes the daemon and bricks the robot.**

Autonomous OS is always **installed on top** of Pollen's OS:

- **First spike**: `bash devices/reachy-mini/spike-hal.sh` — HAL only. Rsyncs
  HAL, installs the device `.env` and `/etc/asound.conf`, borrows camera/audio
  from the daemon, runs uvicorn in tmux. `--stop` gives the media back.
- **Second spike**: `bash devices/reachy-mini/spike-os.sh` — os-server only.
  Cross-compiles for linux/arm64, seeds a minimal `/root/config/config.json`,
  and runs the API in tmux **as root with cwd=/root**: `config.Load` reads the
  relative path `config/config.json`, so any other working directory silently
  points os-server and HAL at different config files.
- **Third spike**: `bash devices/reachy-mini/spike-web.sh` — builds the Vite
  bundle, installs nginx, and writes a spike vhost that serves the bundle and
  proxies `/api/` to os-server. `/hw/` stays **loopback-only** (`allow 127.0.0.1;
  deny all`), matching the production vhost: the browser reaches hardware through
  os-server's authenticated `/api/hardware/*` proxy, never HAL directly.
- **Full spike (legacy)**: `REACHY_HOST=pollen@reachy-mini.local bash devices/reachy-mini/spike.sh`
  — builds HAL + os-server + web in one shot, but installs no nginx and no ALSA
  aliases, so audio and the UI stay dead. Prefer the three focused scripts.
- **Production**: `DEVICE_TYPE=reachy-mini bash <(curl -fsSL .../install.sh)` —
  runs `setup.sh` on the existing OS, adds systemd units, nginx, WiFi AP, OTA.

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

**Contract for HAL**: call `POST /api/media/release` before opening any audio or
camera device, and `POST /api/media/acquire` on shutdown. Until that is wired
into HAL startup, audio/camera routes will fail with "device busy" on a freshly
booted robot. This pairs with — and does not replace — the SDK's
`media_backend="no_media"`, which only stops the *SDK client* from grabbing media.

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
`DEVICE.md` selects it the same way motion picks its driver:

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
Music grooves rotate through `dance1`/`dance2`/`dance3`. Tests guard full
preset coverage and validate all map values against the HF library.

Known deltas from Lamp:

- CSV upload is a Feetech/Lamp animation concept; Reachy's `add_recording` is a
  no-op until we decide whether uploaded moves matter.
- Idle/ambient motion is daemon-owned or recorded-move-library-owned, not the
  Feetech event loop.
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

2. **Quick deploy (recommended for first spike):**

   ```bash
   bash devices/reachy-mini/spike-hal.sh            # HAL only
   bash devices/reachy-mini/spike-hal.sh --no-deps  # redeploy without uv sync
   bash devices/reachy-mini/spike-hal.sh --stop     # stop + return media
   ```

   Defaults to `pollen@reachy-mini.local` (password `root`); direct `root@` SSH
   is refused, so the script sudo's its `apt-get` and `/opt` steps. It rsyncs
   HAL, installs `.env` and `/etc/asound.conf`, syncs the venv (including the
   system libs for pygobject/pycairo), releases the daemon's media, and starts
   uvicorn in tmux. Subsequent runs only sync changed files.

   Use `spike.sh` instead when you also want os-server and the web UI built.

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

4. Boot HAL with the Reachy profile:

   ```bash
   DEVICE_TYPE=reachy-mini DEVICES_DIR=/opt/devices \
     .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001
   ```

4. Confirm mounted routes:

   ```bash
   curl -s http://localhost:5001/device
   curl -s http://localhost:5001/health
   ```

   Expected when all required drivers are available: `audio`, `camera`,
   `emotion`, `servo`, `speaker`, `system`, `voice`. `led` and `display` should
   be absent.

5. Verify motion in safe order:

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

The production `.env` lives at `devices/reachy-mini/rootfs/opt/hal/.env` (rootfs
overlay pattern — same as Lamp). It sets `DEVICE_TYPE=reachy-mini` and tuning
defaults. `spike-hal.sh` copies it — and `/etc/asound.conf` — on first deploy;
`spike.sh` copies only the `.env`; OTA/setup.sh copies the rootfs overlay onto `/`.

Audio names are filled in from recon. Mic and speaker are the **same** USB card
(`card 0: Audio [Reachy Mini Audio], device 0`), so
`devices/reachy-mini/rootfs/etc/asound.conf` aliases both to it, addressed by
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
`hal/board/boards.json` and listing it in this device's `DEVICE.md` `boards`. Its
`led`/`button` wiring is inherited from `raspberry_pi_4` and is **unverified** —
Reachy declares neither `light` nor a GPIO button, so nothing reads it yet.
Verify before wiring either peripheral on a CM4.

## Hardware Spike TODOs

Resolved by the 2026-07-29 recon: ALSA names, camera hardware type, board id,
network stack, daemon port/API. Still open:

- wire `POST /api/media/release` / `acquire` into HAL startup/shutdown
- pick a camera path (picamera2 vs daemon-mediated vs `rpicam-vid` subprocess)
- sign convention for `head_yaw.pos`, `head_pitch.pos`, and antenna order
- whether `wake_up` / `goto_sleep` produce sound with `media_backend="no_media"`
- first-run behavior of `pollen-robotics/reachy-mini-emotions-library`
- verify emotion→HF move mapping looks/feels right on the robot
- re-test acoustic echo cancellation / barge-in (shared USB clock, unlike Lamp)
- thermal limits before enabling `SAFETY.md` `thermal`
- `uv` is not installed on Pollen OS; both spike scripts install it, but
  production `setup.sh` must too
- neither spike script is production: no systemd, no nginx, no OTA
- `setup.sh` must take the NetworkManager branch (`nmcli`), and can reuse the
  existing `Hotspot` profile (`reachy-mini-ap`, `ipv4=shared`) instead of
  installing hostapd/dnsmasq
