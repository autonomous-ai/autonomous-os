# Servo Calibration

LeLamp uses 5x Feetech STS3215 servos controlled via the `lerobot` library. Each servo requires calibration to map raw encoder positions to degrees. Calibration data is stored as JSON and loaded at startup.

## Calibration files

lerobot looks for calibration at `~/.cache/huggingface/lerobot/calibration/` for the user running the service:

```
<user_home>/.cache/huggingface/lerobot/calibration/
├── robots/lelamp_follower/lelamp.json
└── teleoperators/lelamp_leader/lelamp.json
```

| Device | Service user | Calibration path |
|--------|-------------|-----------------|
| lumi2 / lumi3 (RPi 5) | `root` | `/root/.cache/huggingface/lerobot/calibration/` |
| lumi4 (OrangePi 4P) | `root` | `/root/.cache/huggingface/lerobot/calibration/` |
| lumi_final (OrangePi) | `orangepi` | `/home/orangepi/.cache/huggingface/lerobot/calibration/` |

Each JSON contains per-servo values:

| Field | Description |
|-------|-------------|
| `id` | Servo ID (1–5) |
| `drive_mode` | Direction (0 = normal) |
| `homing_offset` | Encoder offset for center position |
| `range_min` | Minimum encoder value (physical limit) |
| `range_max` | Maximum encoder value (physical limit) |

## Servos

| ID | Name | Function |
|----|------|----------|
| 1 | `base_yaw` | Left/right rotation |
| 2 | `base_pitch` | Forward/backward tilt |
| 3 | `elbow_pitch` | Elbow bend |
| 4 | `wrist_roll` | Wrist rotation |
| 5 | `wrist_pitch` | Wrist tilt |

## Deploy to a device (copy existing calibration)

If the device uses the same servo hardware as an already-calibrated lamp, copy the calibration files.

**Root-user devices (lumi2, lumi3, lumi4):**
```bash
sudo mkdir -p /root/.cache/huggingface/lerobot/calibration/robots/lelamp_follower
sudo mkdir -p /root/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader
sudo cp calibration/robots/lelamp_follower/lelamp.json \
  /root/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lelamp.json
sudo cp calibration/teleoperators/lelamp_leader/lelamp.json \
  /root/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lelamp.json
```

**orangepi-user devices (lumi_final):**
```bash
mkdir -p /home/orangepi/.cache/huggingface/lerobot/calibration/robots/lelamp_follower
mkdir -p /home/orangepi/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader
cp calibration/robots/lelamp_follower/lelamp.json \
  /home/orangepi/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lelamp.json
cp calibration/teleoperators/lelamp_leader/lelamp.json \
  /home/orangepi/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lelamp.json
```

Then restart the lelamp service. The servos will use the copied calibration values.

> **Note:** The calibration files in this branch (`feat/lumi-final`) are tuned for **lumi_final's** servo hardware. The `wrist_pitch` recordings are scaled to ±55.8° to match lumi_final's physical range (vs ±113° on the original hardware).

## Run fresh calibration on a Pi

Use this when setting up a new lamp or after replacing servos.

```bash
# Follower only
sudo /opt/hal/.venv/bin/python3 -m hal.calibrate \
  --id lelamp --port /dev/ttyACM0 --follower-only

# Leader only
sudo /opt/hal/.venv/bin/python3 -m hal.calibrate \
  --id lelamp --port /dev/ttyACM0 --leader-only

# Both follower and leader
sudo /opt/hal/.venv/bin/python3 -m hal.calibrate \
  --id lelamp --port /dev/ttyACM0
```

### Calibration steps (interactive)

1. **Torque is disabled** — servos go limp so you can move them by hand.
2. **Move all joints to the middle** of their range of motion, then press ENTER. This sets the homing offset.
3. **Move each joint through its full range** (min to max). The script records encoder positions. Press ENTER when done.
4. Calibration is saved to `/root/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lelamp.json`.

### After calibration

- Back up the new calibration file to this repo under `calibration/`.
- Restart the lelamp service: `sudo systemctl restart lelamp` (or reboot).
