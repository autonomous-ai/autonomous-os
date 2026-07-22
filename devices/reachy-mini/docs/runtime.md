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

Reachy Mini ships with **Pollen's OS** (Debian custom) on its onboard Pi. The OS
includes a daemon that owns the serial bus, runs the 100 Hz control loop,
computes inverse kinematics for the Stewart platform, and enforces hardware
safety clamps. **Flashing a golden image wipes the daemon and bricks the robot.**

Autonomous OS is always **installed on top** of Pollen's OS:

- **Spike/dev**: `REACHY_HOST=pi@<IP> bash devices/reachy-mini/spike.sh` — builds
  on Mac, rsync to Pi, starts HAL + os-server in tmux.
- **Production**: `DEVICE_TYPE=reachy-mini bash <(curl -fsSL .../install.sh)` —
  runs `setup.sh` on the existing OS, adds systemd units, nginx, WiFi AP, OTA.

Two stacks run side by side on the same Pi:

| Layer | Owner | How |
|-------|-------|-----|
| Motion (head 6-DOF, body 360°, antennas) | Pollen daemon `:8000` | HAL calls SDK → daemon owns hardware I/O |
| Audio (mic, speaker, TTS) | HAL directly | ALSA/PulseAudio, `media_backend="no_media"` |
| Camera | HAL directly | V4L2/OpenCV |
| Agent brain, setup, OTA | os-server + OpenClaw | Independent of Pollen |

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
   REACHY_HOST=pi@<IP> bash devices/reachy-mini/spike.sh
   ```

   This builds on Mac, rsync to Pi, installs all deps (including system libs
   for pygobject/pycairo), and starts HAL + os-server in a tmux session.
   Subsequent runs only sync changed files.

3. **Manual install** (if not using spike.sh):

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
defaults. `spike.sh` copies it on first deploy; OTA/setup.sh copies the rootfs
overlay onto `/`.

Audio device names are `TODO(spike)`. After first boot, run on the Pi:

```bash
arecord -l   # find mic array card
aplay -l     # find speaker card
```

Then create `devices/reachy-mini/rootfs/etc/asound.conf` with ALSA aliases
(same pattern as Lamp's `plug:device_micro` / `plug:device_speaker`), and
update `.env` to reference them:

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker
```

Camera: run `v4l2-ctl --list-devices` and update `HAL_CAMERA_INDEX` in `.env`.

## Hardware Spike TODOs

Update this doc after the first real-device session:

- ALSA device names → create `rootfs/etc/asound.conf` + update `.env`
- camera device index and resolution → update `.env`
- actual HAL board id for the Wireless model (`DEVICE.md` currently allows
  `raspberry_pi_4` and `raspberry_pi_5`)
- sign convention for `head_yaw.pos`, `head_pitch.pos`, and antenna order
- whether `wake_up` / `goto_sleep` produce sound with `media_backend="no_media"`
- first-run behavior of `pollen-robotics/reachy-mini-emotions-library`
- verify emotion→HF move mapping looks/feels right on the robot
- thermal limits before enabling `SAFETY.md` `thermal`
