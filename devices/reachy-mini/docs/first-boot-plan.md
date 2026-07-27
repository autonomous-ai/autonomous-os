# Reachy Mini First-Boot Plan

Step-by-step plan for the first real-device session. Run once when the Wireless
unit arrives, then update `runtime.md`, `.env`, and `setup.sh` with the findings.

## Phase 1: SSH Recon (Read-Only)

SSH in and collect system info. **Do not change anything yet.**

```bash
ssh pollen@reachy-mini.local   # password: root
```

**Shortcut:** [`../recon.sh`](../recon.sh) runs every command in this phase in one
shot and prints a fill-in summary. Prefer it over typing the sections by hand:

```bash
scp devices/reachy-mini/recon.sh pollen@reachy-mini.local:/tmp/
ssh pollen@reachy-mini.local 'bash /tmp/recon.sh' | tee reachy-recon.txt
# add --audio-test to also run the 3s mic->speaker loopback (the only step that makes sound)
```

The manual sections below document what each probe checks and why.

### 1.1 OS & Kernel

```bash
cat /etc/os-release              # Bookworm or Bullseye?
uname -a                        # kernel version, arch
cat /boot/firmware/config.txt 2>/dev/null || cat /boot/config.txt
df -h                           # disk usage (16 GB eMMC)
free -h                         # RAM
```

**Why**: determines package availability, dtoverlay syntax, and whether
`/boot/firmware/` or `/boot/` is the config path.

### 1.2 Network Stack

```bash
systemctl is-active NetworkManager
systemctl is-active dhcpcd
systemctl is-active wpa_supplicant
systemctl is-active systemd-networkd
nmcli device status 2>/dev/null || echo "No NetworkManager"
ip addr show wlan0
cat /etc/wpa_supplicant/*.conf 2>/dev/null
cat /etc/dhcpcd.conf 2>/dev/null
ls /etc/NetworkManager/system-connections/ 2>/dev/null
```

**Why**: the single most important check. Determines whether our `setup.sh` can
reuse the existing dhcpcd/wpa_supplicant flow or needs a NetworkManager-aware
path. See [recovery.md](recovery.md) for risk analysis.

**Decision tree**:

```
NetworkManager active?
├── YES → write NM-aware setup.sh (nmcli for AP/STA, skip hostapd)
│         OR disable NM and install dhcpcd stack (riskier)
└── NO → dhcpcd active?
    ├── YES → current setup.sh works as-is
    └── NO → systemd-networkd? custom? → investigate
```

### 1.3 Pollen Daemon

```bash
systemctl list-units | grep -i reachy
systemctl list-units | grep -i pollen
systemctl status reachy*
curl -s http://localhost:8000 | head -20
curl -s http://localhost:8000/api 2>/dev/null | head -20
ls /venvs/
ls /restore/venvs/
pip list 2>/dev/null | grep -i reachy
cat /etc/systemd/system/reachy* 2>/dev/null
```

**Why**: need to know the exact service name, port, API surface, and venv
layout so our HAL driver and setup.sh don't collide.

**Record**:
- [ ] Daemon service name: `_______________`
- [ ] Daemon port: `_______________`
- [ ] Daemon API base path: `_______________`
- [ ] Python version in `/venvs/`: `_______________`
- [ ] `/restore/venvs/` exists: yes / no

### 1.4 Audio

```bash
arecord -l                       # list capture devices (mic array)
aplay -l                        # list playback devices (speaker)
cat /proc/asound/cards
cat /etc/asound.conf 2>/dev/null
# Quick test (record 3s, play back)
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:1,0 /tmp/test.wav
```

**Record**:
- [ ] Mic ALSA name: `_______________` (e.g. `plughw:2,0`)
- [ ] Speaker ALSA name: `_______________` (e.g. `plughw:0,0`)
- [ ] Mic channels: `_______________`
- [ ] Sample rate works at 16 kHz: yes / no

### 1.5 Camera

```bash
v4l2-ctl --list-devices
ls /dev/video*
# Quick test
v4l2-ctl -d /dev/video0 --all 2>/dev/null | head -30
# If libcamera is used instead of V4L2:
libcamera-hello --list-cameras 2>/dev/null
```

**Record**:
- [ ] Camera device index: `_______________`
- [ ] V4L2 or libcamera: `_______________`
- [ ] Max resolution: `_______________`

### 1.6 Existing Services & Ports

```bash
ss -tlnp                        # all listening TCP ports
systemctl list-units --type=service --state=running
# Check for port conflicts with our services
# HAL: 5001, os-server: 8080, nginx: 80
```

**Record**:
- [ ] Port 5001 free: yes / no
- [ ] Port 8080 free: yes / no
- [ ] Port 80 free: yes / no (nginx?)

### 1.7 System Dependencies

```bash
# Check if pygobject/pycairo build deps exist
dpkg -l | grep -E 'libcairo2-dev|libgirepository|pkg-config'
python3 --version
which uv 2>/dev/null
which pip3
```

### 1.8 Bluetooth

```bash
# Check BLE service for recovery
systemctl status bluetooth
hciconfig -a 2>/dev/null
```

## Phase 2: Write Configs Based on Findings

After Phase 1, update these files **on the dev machine** (not on the Pi):

### 2.1 ALSA Config

Create `devices/reachy-mini/rootfs/etc/asound.conf` with actual device names:

```
# Template — fill after arecord -l / aplay -l
pcm.device_mic {
    type plug
    slave.pcm "hw:<CARD>,<DEV>"
}

pcm.device_speaker {
    type plug
    slave.pcm "hw:<CARD>,<DEV>"
}
```

### 2.2 HAL .env

Update `devices/reachy-mini/rootfs/opt/hal/.env` with actual values:

```bash
HAL_AUDIO_INPUT_ALSA=plug:device_mic       # from 1.4
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker   # from 1.4
HAL_CAMERA_INDEX=0                          # from 1.5
```

### 2.3 setup.sh (New, Reachy-Specific)

Write `devices/reachy-mini/setup.sh` (or modify shared `scripts/provision/setup.sh`
with `DEVICE_TYPE` branching). Key design decisions from recon:

| Decision | If NM | If dhcpcd |
|----------|-------|-----------|
| AP mode | `nmcli` hotspot or install hostapd | Existing hostapd flow |
| STA mode | `nmcli con add` | Existing wpa_supplicant flow |
| DNS captive portal | dnsmasq drop-in (same) | dnsmasq drop-in (same) |
| Service masking | Don't mask NM, configure it | Mask global wpa_supplicant (same) |

Regardless of network stack, setup.sh must:

1. **Never stop or restart the Pollen daemon** during install
2. **Install into a separate venv** (`/opt/hal/.venv/`, not `/venvs/`)
3. **Install system deps**: `libcairo2-dev`, `libgirepository1.0-dev`, `pkg-config`
4. **Not conflict on ports**: verify 5001, 8080, 80 are free before binding
5. **Set hostname** to `reachy-mini-<suffix>` without breaking Pollen's mDNS
6. **Create systemd units** for `hal.service` and `os-server.service`
7. **Install nginx** with captive portal config (or skip if Pollen already runs nginx)

### 2.4 HAL .env Production Plan

Current `.env` at `devices/reachy-mini/rootfs/opt/hal/.env` has several
`TODO(spike)` placeholders. After Phase 1 recon, fill in the actual values.

**Full .env field plan** (fields marked `?` need real-device data):

```bash
# --- Core ---
HAL_MODE=production
HAL_LOG_LEVEL=INFO
DEVICE_TYPE=reachy-mini
DEVICES_DIR=/opt/devices

# --- Pollen daemon ---
# Recon 1.3: confirm host/port. Currently commented out (defaults to
# localhost:8000 in reachy_service.py). Uncomment only if daemon runs
# on a different host or port.
#REACHY_DAEMON_HOST=localhost          # ? confirm daemon is on localhost
#REACHY_DAEMON_PORT=8000              # ? confirm port from `ss -tlnp`

# --- Audio ---
# Recon 1.4: fill from `arecord -l` / `aplay -l`
HAL_AUDIO_INPUT_ALSA=plug:device_mic  # ? actual ALSA name after asound.conf
HAL_AUDIO_OUTPUT_ALSA=plug:device_speaker  # ? actual ALSA name
HAL_VAD_THRESHOLD=500                 # may need tuning for 4-mic array
HAL_SPEECH_HOLDOFF=0.05
HAL_SILENCE_TIMEOUT=3.0
HAL_STT_KEEPALIVE=false
HAL_SILERO_ENABLED=true
HAL_SILERO_THRESHOLD=0.15             # ? tune on real hardware
HAL_SILERO_CHUNK_SIZE=512
HAL_WEBRTCVAD_ENABLED=true
HAL_WEBRTCVAD_AGGRESSIVENESS=0        # ? may need higher for noisy servo motors
HAL_WEBRTCVAD_FRAME_MS=30
HAL_TTS_SPEED=1.1

# --- Camera ---
# Recon 1.5: confirm index from `v4l2-ctl --list-devices`
HAL_CAMERA_INDEX=0                    # ? confirm device index
HAL_CAMERA_WIDTH=1280                 # ? confirm max usable resolution
HAL_CAMERA_HEIGHT=720                 # ? with daemon CPU sharing
HAL_CAMERA_STREAM_WIDTH=960
HAL_CAMERA_STREAM_HEIGHT=540
HAL_CAMERA_AUTO_EXPOSURE=auto
# NOTE: if Pollen OS uses libcamera instead of V4L2, HAL's OpenCV backend
# may need LIBCAMERA_LOG_LEVELS=ERROR or a gstreamer pipeline. Check 1.5.

# --- Sensing ---
HAL_MOTION_ENABLED=true
HAL_EMOTION_ENABLED=true
HAL_POSE_MOTION_ENABLED=false         # no pose tracking until CPU budget known
HAL_MOTION_CONFIDENCE_THRESHOLD=0.4
HAL_EMOTION_CONFIDENCE_THRESHOLD=0.5
HAL_DL_ENCRYPTION=true
HAL_DL_ENCRYPTION_REQUIRED=false
SPEAKER_MATCH_THRESHOLD=0.7
SPEAKER_ENROLL_CONSISTENCY_THRESHOLD=0.7

# --- Realtime voice ---
HAL_REALTIME_TURN_DETECTION=off
HAL_WARM_MIC=true
HAL_WARM_MIC_ECHO_SKIP_MAX_S=0.1
HAL_ECHO_RMS_FLOOR=300                # ? tune: Reachy's speaker is 5W,
                                      #   may need different floor vs Lamp's 3W

# --- CPU tuning ---
# RPi CM4: 4 cores shared with Pollen daemon (100 Hz control loop).
# Keep thread count low to avoid starving the daemon.
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
# ? After thermal recon: consider adding CPU governor or thermal throttle
```

**Key tuning questions for real hardware**:

1. **VAD threshold**: Reachy's servo motors may produce more mechanical noise
   than Lamp's Feetech servos. May need `HAL_WEBRTCVAD_AGGRESSIVENESS=1` or
   higher.
2. **Echo floor**: 5W speaker vs Lamp's 3W — `HAL_ECHO_RMS_FLOOR` may need
   raising to avoid self-triggering during TTS playback.
3. **Camera**: if Pi Camera v3 uses libcamera, OpenCV `VideoCapture(index)` may
   not work. Need gstreamer pipeline or `picamera2` integration.
4. **CPU budget**: Pollen daemon runs a 100 Hz control loop. Our sensing
   (emotion + motion detection) adds CPU load. Monitor with `htop` during
   concurrent motion + inference. If CPU > 80%, disable `HAL_POSE_MOTION_ENABLED`
   and reduce camera resolution.

### 2.5 Motion Driver TODOs

After Phase 1, resolve `TODO(spike)` items in `reachy_service.py`:

- [ ] Verify `wake_up()` / `goto_sleep()` behavior
- [ ] Verify sign conventions (positive yaw = left or right?)
- [ ] Test all 28 emotion→HF move mappings
- [ ] Check if recorded moves need Hugging Face network access on first run
- [ ] Measure thermal profile under load

## Phase 3: Deploy & Validate

### 3.1 Spike Deploy

```bash
REACHY_HOST=pollen@<IP> bash devices/reachy-mini/spike.sh
```

### 3.2 Smoke Test

```bash
# Health
curl -s http://<IP>:5001/health
curl -s http://<IP>:5001/device

# Motion (safe order)
curl -s http://<IP>:5001/servo/position
curl -s -X POST http://<IP>:5001/servo/aim \
  -H 'content-type: application/json' \
  -d '{"direction":"center","duration":1.0}'
curl -s -X POST http://<IP>:5001/servo/zero
curl -s -X POST http://<IP>:5001/servo/release

# Audio
curl -s -X POST http://<IP>:5001/speaker/play \
  -H 'content-type: application/json' \
  -d '{"text":"Hello, I am Reachy"}'

# Camera
curl -s http://<IP>:5001/camera/snapshot -o /tmp/snap.jpg
open /tmp/snap.jpg
```

### 3.3 Production Setup Test

Only after spike works:

```bash
ssh pollen@<IP>
DEVICE_TYPE=reachy-mini bash setup.sh   # the new one
# Reboot
sudo reboot
# Verify AP mode comes up
# Connect phone to reachy-mini-xxxx AP
# Complete setup flow via captive portal
```

## Phase 4: Update Docs

After everything works, update:

- [ ] `devices/reachy-mini/docs/runtime.md` — fill all TODO(spike) items
- [ ] `devices/reachy-mini/docs/vi/runtime_vi.md` — Vietnamese mirror
- [ ] `devices/reachy-mini/docs/recovery.md` — confirm BLE commands work
- [ ] `devices/reachy-mini/docs/vi/recovery_vi.md` — Vietnamese mirror
- [ ] `devices/reachy-mini/rootfs/opt/hal/.env` — actual values
- [ ] `devices/reachy-mini/rootfs/etc/asound.conf` — actual ALSA names
- [ ] `CLAUDE.md` — if new docs are created

## Phase 5: Golden Base Image (Capture from Device) & build-reachy.sh

The Reachy image build follows the **same pattern lamp/intern-v2 already use**:
the imager does not build on a stock vendor image — it builds on a base image
**captured from a known-good device**. See `scripts/imager/README.md`
("Base image — per device type"). The Pollen GitHub release image
(`recovery.md` Level D) is a **recovery fallback only**, not the build base.

### 5.1 Capture the base image FROM the device

Do this **before running `setup.sh`**, while the eMMC is still pristine Pollen OS
(Phase 1 recon is read-only, so it's fine to recon first, then capture). The CM4
eMMC has no SD slot, so capture goes through the same rpiboot USB path as a
reflash (`recovery.md` Level D) — but **reading** instead of writing:

```bash
# 1. shutdown robot → switch SW1 to DOWNLOAD → connect USB2 → start rpiboot → power on
sudo ./rpiboot -d mass-storage-gadget64          # eMMC appears as /dev/diskX (macOS) or /dev/sdX (Linux)

# 2. unmount the auto-mounted partitions first
sudo diskutil unmountDisk /dev/diskX             # macOS
# sudo umount /media/$USER/bootfs /media/$USER/rootfs   # Linux

# 3. raw-read the WHOLE disk (partition table included) and compress on the fly
sudo dd if=/dev/rdiskX bs=8m | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz    # macOS (note rdiskX)
# sudo dd if=/dev/sdX bs=8M | xz -T0 -c > reachy-mini-base-v<pollen-ver>.img.xz     # Linux
```

Store it as the imager base, following the lamp/intern layout:
`scripts/imager/input/reachy-mini/golden-reachy-dev.img.xz`. Optionally mirror it
to the Autonomous CDN like the other bases:
`gs://s3-autonomous-upgrade-3/os/imager/base/golden-reachy-dev.img.xz`.

### 5.2 Recover the device FROM the captured base

Same rpiboot USB path as 5.1, writing the captured image back. Two options:

```bash
# --- Option A: dd (simplest — the capture was a dd, so dd restores it verbatim) ---
# shutdown → SW1 DOWNLOAD → USB2 → rpiboot → power on → unmount (as in 5.1)
xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/rdiskX bs=8m    # macOS
# xz -dc reachy-mini-base-v<pollen-ver>.img.xz | sudo dd of=/dev/sdX bs=8M     # Linux

# --- Option B: bmaptool (faster, sparse — generate a .bmap once, then flash) ---
xz -dc reachy-mini-base-v<pollen-ver>.img.xz > reachy-base.img
bmaptool create -o reachy-base.bmap reachy-base.img
sudo bmaptool copy reachy-base.img --bmap reachy-base.bmap /dev/rdiskX
```

Then restore normal boot: power off → switch back to **DEBUG** → disconnect USB →
power on. Verify with `reachyminios_check` (should print `Image validation PASSED`)
and confirm the robot actually moves correctly (calibration check, see 5.3).

### 5.3 Why capture, not download the Pollen release

- The shipped eMMC may carry drivers, config, or first-boot state the generic
  release lacks — capturing guarantees the base matches the exact hardware and
  daemon the unit runs.
- Same rationale lamp/intern-v2 use the hardware team's image over the stock `.7z`.
- **Caveat — per-unit state**: a dump of one unit's eMMC may include per-robot
  calibration (servo/IMU offsets) or identity. Restoring onto the **same** unit
  is always safe. Before reusing a captured image as the base for **other** units,
  verify what is per-unit and strip/regenerate it — recon question: does
  `reachyminios_check` pass **and** does the robot move correctly after flashing a
  captured image onto a *different* unit?

### 5.4 build-reachy.sh (future imager target — not yet written)

Would mirror `build-orangepi.sh` phases, adapted:

| build-orangepi.sh | build-reachy.sh delta |
|---|---|
| Phase 0 base: `gdown` stock `.7z` | decompress `input/reachy-mini/golden-reachy-dev.img.xz` (captured in 5.1) |
| Phase 2 chroot apt: hostapd/dnsmasq/dhcpcd | **gated on recon 1.2** — NetworkManager vs dhcpcd decides the network packages |
| Phase 2: bake full OS stack | **install on top — never wipe the Pollen daemon** |
| Flash: SD card via Imager | **rpiboot + bmaptool to eMMC** — no SD slot |
| Phase 3 OTA bake | same: os-server, bootstrap, HAL, device profile overlay |

Blocked on: recon 1.2 (network stack) and the per-unit-state question (5.3). Only
worth building when shipping multiple Reachy units; for a single dev unit,
`spike.sh` + `setup.sh`-on-top is enough.

## References

- [Pollen OS build system](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini SDK](https://github.com/pollen-robotics/reachy_mini)
- [Hardware datasheet](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Reflash guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [BLE reset](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Autonomous setup.sh](../../scripts/provision/setup.sh)
- [Autonomous imager](../../scripts/imager/README.md)
