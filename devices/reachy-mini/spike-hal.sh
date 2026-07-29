#!/usr/bin/env bash
# spike-hal.sh — Deploy and run ONLY the HAL runtime on a Reachy Mini.
#
# Runs FROM YOUR MAC. Unlike spike.sh (which also builds os-server + the web UI),
# this script targets the body alone: rsync HAL, install the device .env and ALSA
# aliases, sync the Python venv, borrow the media devices from the Pollen daemon,
# and run uvicorn under a systemd unit. Use it to validate motion, audio, and the
# device profile before any of the agent stack exists on the robot.
#
# Why a unit and not tmux: a tmux session dies with the SSH connection and never
# comes back after a power cut, so the robot came up mute and blind every time it
# was unplugged. The unit is still not production — it points at the spike layout
# (/opt/autonomous), not the OTA layout (/opt/hal) that scripts/provision/setup.sh
# installs — but the stack survives a reboot. No nginx, no OTA.
#
# Why HAL-only: os-server binds 127.0.0.1:5000 and does not serve static files —
# the web UI needs nginx (see scripts/provision/setup.sh), which the Reachy
# provisioning path does not have yet. Deploying them here would install two
# processes nobody can reach.
#
# Prerequisites:
#   - SSH access to the robot; the login needs passwordless sudo. On the shipped
#     Pollen OS that is `pollen` (root cannot SSH in directly).
#   - Pollen daemon running and healthy on localhost:8000.
#
# Usage:
#   bash devices/reachy-mini/spike-hal.sh                 # full deploy + run
#   bash devices/reachy-mini/spike-hal.sh --no-deps       # skip uv sync (fast redeploy)
#   bash devices/reachy-mini/spike-hal.sh --keep-media    # do not release daemon media
#   bash devices/reachy-mini/spike-hal.sh --stop          # stop HAL, give media back
#   bash devices/reachy-mini/spike-hal.sh --uninstall     # stop + remove the unit
#
#   REACHY_HOST=pollen@172.168.20.208 bash devices/reachy-mini/spike-hal.sh
set -euo pipefail

REACHY_HOST="${REACHY_HOST:-pollen@reachy-mini.local}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_BASE="/opt/autonomous"
DAEMON="http://localhost:8000"
SERVICE="hal"
# HAL runs as root (config.py hardcodes state paths under /root), but the model
# caches were all built under the login user, because this script used to run
# uvicorn with `sudo -E`, which keeps HOME. systemd gives root HOME=/root, so
# without pinning this the whole model set downloads again onto an eMMC that is
# already ~86% full.
HF_HOME_PATH="/home/pollen/.cache/huggingface"

SKIP_DEPS=0
KEEP_MEDIA=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-deps)    SKIP_DEPS=1 ;;
    --keep-media) KEEP_MEDIA=1 ;;
    --stop)       STOP_ONLY=1 ;;
    --uninstall)  UNINSTALL=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n========== %s ==========\n' "$1"; }

# --- teardown ---------------------------------------------------------------
# Stopping HAL is not enough: the daemon must take its camera and audio back, or
# Pollen's own app stack stays deaf and blind until someone calls acquire. The
# unit does that itself via ExecStopPost; the explicit call here covers a HAL
# that was killed hard enough to skip it.
if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping HAL on $REACHY_HOST"
  ssh "$REACHY_HOST" bash <<REMOTE_STOP
sudo systemctl stop $SERVICE 2>/dev/null || true
# Legacy path: earlier versions of this script ran HAL in tmux.
tmux kill-session -t $SERVICE 2>/dev/null || true
if [ "$UNINSTALL" = "1" ]; then
  sudo systemctl disable $SERVICE 2>/dev/null || true
  sudo rm -f /etc/systemd/system/$SERVICE.service
  sudo systemctl daemon-reload
  echo "[spike-hal] unit removed"
fi
sleep 1
curl -s -X POST $DAEMON/api/media/acquire >/dev/null || true
echo -n "[spike-hal] media: "; curl -s $DAEMON/api/media/status; echo
REMOTE_STOP
  echo
  if [ "$UNINSTALL" = "1" ]; then
    echo "HAL stopped and the unit removed; media handed back to the daemon."
  else
    echo "HAL stopped, media handed back. Still enabled — it returns on the next boot."
    echo "To keep it down: bash devices/reachy-mini/spike-hal.sh --uninstall"
  fi
  exit 0
fi

echo "target: $REACHY_HOST   remote base: $REMOTE_BASE"

say "0/5  Preflight: disk space"
# The HAL venv is ~2 GB (torch, opencv, polars, pyarrow) and uv's wheel cache can
# add as much again. The robot ships a 14 GB eMMC that is already ~60% full, and
# filling it would hurt the Pollen daemon (journal, state writes) far more than
# anything else this script does. Refuse rather than wedge the robot.
#
# Only gate the path that actually downloads: with --no-deps the venv already
# exists and nothing new is fetched, so a full disk is not this run's problem.
if [ "$SKIP_DEPS" = "1" ]; then
  echo "[spike-hal] --no-deps: skipping the disk gate (no downloads this run)"
  ssh "$REACHY_HOST" "df -h / | awk 'NR==2 {print \"[spike-hal] free space on /: \" \$4}'"
else
ssh "$REACHY_HOST" bash <<'REMOTE_DISK'
set -e
avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
avail_gb=$(awk -v k="$avail_kb" 'BEGIN {printf "%.1f", k/1048576}')
echo "[spike-hal] free space on /: ${avail_gb} GB"
if [ "$avail_kb" -lt 4194304 ]; then
  echo "[spike-hal] ABORT: need at least 4 GB free for the HAL venv + uv cache."
  echo "[spike-hal] Free space first (uv cache clean, journalctl --vacuum-size=100M),"
  echo "[spike-hal] or run with --no-deps if the venv already exists."
  exit 1
fi
REMOTE_DISK
fi

say "1/5  Copy HAL + device profile"
# /opt is root-owned; create the tree with sudo once, then own it as the login
# user so rsync and uv need no privileges.
ssh "$REACHY_HOST" "sudo mkdir -p $REMOTE_BASE/{hal,devices} && sudo chown -R \$(id -u):\$(id -g) $REMOTE_BASE"

rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '*.pyc' --exclude 'test/' --exclude '.env' \
  "$ROOT_DIR/hal/" "$REACHY_HOST:$REMOTE_BASE/hal/"

rsync -az --delete "$ROOT_DIR/devices/reachy-mini/" "$REACHY_HOST:$REMOTE_BASE/devices/reachy-mini/"

say "2/5  Install device config (.env + ALSA aliases)"
# .env is copied only when absent so values tuned on the robot survive redeploys.
# asound.conf must land in /etc — HAL asks ALSA for `plug:device_mic`, which does
# not exist until this file is in place.
ssh "$REACHY_HOST" bash <<REMOTE_CONF
set -e
BASE="$REMOTE_BASE"
if [ -f "\$BASE/hal/.env" ]; then
  echo "[spike-hal] .env already present — keeping it"
else
  cp "\$BASE/devices/reachy-mini/rootfs/opt/hal/.env" "\$BASE/hal/.env"
  echo "[spike-hal] installed .env from the rootfs overlay"
fi

# The overlay ships the production DEVICES_DIR=/opt/devices, but the spike keeps
# everything under one tree so \`rm -rf $REMOTE_BASE\` reclaims it all. Point it
# at what was actually deployed.
#
# This has to be the .env and not Environment= in the unit: systemd applies
# EnvironmentFile= AFTER Environment=, so the file wins no matter which order
# they appear in. Getting it wrong is silent in the worst way — HAL refuses to
# boot with "DEVICE.md required but not loaded", or, if the path happens to
# exist, every service reports healthy while the capability list comes back empty.
if grep -q '^DEVICES_DIR=' "\$BASE/hal/.env"; then
  sed -i "s|^DEVICES_DIR=.*|DEVICES_DIR=\$BASE/devices|" "\$BASE/hal/.env"
else
  echo "DEVICES_DIR=\$BASE/devices" >> "\$BASE/hal/.env"
fi
echo "[spike-hal] .env DEVICES_DIR -> \$BASE/devices"
# Pollen OS ships no /etc/asound.conf, so this creates rather than replaces. Back
# up anyway: a future Pollen image may add one, and this script must never be the
# reason their audio config disappeared.
if [ -f /etc/asound.conf ] \
   && ! cmp -s /etc/asound.conf "\$BASE/devices/reachy-mini/rootfs/etc/asound.conf" \
   && [ ! -f /etc/asound.conf.pre-autonomous ]; then
  sudo cp /etc/asound.conf /etc/asound.conf.pre-autonomous
  echo "[spike-hal] backed up the existing /etc/asound.conf -> .pre-autonomous"
fi
sudo cp "\$BASE/devices/reachy-mini/rootfs/etc/asound.conf" /etc/asound.conf
echo "[spike-hal] installed /etc/asound.conf"
REMOTE_CONF

if [ "$SKIP_DEPS" = "0" ]; then
  say "3/5  Install deps (uv, cairo libs, HAL venv)"
  ssh "$REACHY_HOST" bash <<'REMOTE_DEPS'
set -e
export PATH="$HOME/.local/bin:$PATH"

command -v uv &>/dev/null || { echo "[spike-hal] installing uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }

# pygobject/pycairo build deps for the `reachy` extra. Pollen OS ships them, but
# a fresh image or another unit may not.
sudo apt-get install -y --no-install-recommends \
  libcairo2-dev libgirepository1.0-dev pkg-config 2>/dev/null \
  || echo "[spike-hal] WARN: apt install failed — pygobject may fail to build"

# Keep uv's wheel cache inside our tree: `rm -rf /opt/autonomous` then reclaims
# every byte this script wrote, instead of leaving GBs in ~/.cache/uv.
export UV_CACHE_DIR=/opt/autonomous/.uv-cache

cd /opt/autonomous/hal

# Retry the sync: pulling ~2 GB of wheels over office WiFi times out often enough
# that a single attempt is not a real deploy step. uv resumes from its cache, so
# a retry only fetches what is still missing. 30s is uv's default per-request
# timeout — too tight for a 300 MB torch wheel on a shared link.
export UV_HTTP_TIMEOUT=180
sync_ok=0
for attempt in 1 2 3; do
  echo "[spike-hal] uv sync attempt $attempt/3"
  if uv sync --python 3.12 --extra hardware --extra reachy; then
    sync_ok=1
    break
  fi
  echo "[spike-hal] attempt $attempt failed — retrying in 5s"
  sleep 5
done
[ "$sync_ok" = "1" ] || { echo "[spike-hal] ABORT: uv sync failed 3 times (network?). Cache is kept — rerun to resume."; exit 1; }

# Drop cache entries no longer referenced by the venv — the download cache is
# dead weight on a 14 GB eMMC once the venv is built.
uv cache prune 2>/dev/null || true
df -h / | awk 'NR==2 {print "[spike-hal] free space after sync: " $4}'
REMOTE_DEPS
else
  say "3/5  Deps skipped (--no-deps)"
fi

say "4/5  Borrow camera + audio from the Pollen daemon"
# The daemon holds /dev/video* and both ALSA PCMs while it runs, so HAL cannot
# open them. `release` is the daemon's supported handover; motion is unaffected.
# This is a spike-time workaround — HAL should call it itself once wired in.
if [ "$KEEP_MEDIA" = "1" ]; then
  echo "[spike-hal] --keep-media: leaving media with the daemon (audio/camera will fail with 'device busy')"
else
  ssh "$REACHY_HOST" bash <<REMOTE_MEDIA
set -e
curl -s -X POST $DAEMON/api/media/release >/dev/null || { echo "[spike-hal] WARN: media release failed — is the daemon up?"; exit 0; }
sleep 1
echo -n "[spike-hal] media status: "; curl -s $DAEMON/api/media/status; echo
echo -n "[spike-hal] mic check: "
if arecord -D plug:device_mic -f S16_LE -r 16000 -c 1 -d 1 /tmp/_spike_mic.wav >/dev/null 2>&1; then
  echo "OK (recorded 1s through plug:device_mic)"
else
  echo "FAILED — check /etc/asound.conf and 'arecord -l'"
fi
rm -f /tmp/_spike_mic.wav
REMOTE_MEDIA
fi

say "5/5  Install the systemd unit and start HAL"
ssh "$REACHY_HOST" bash <<REMOTE_START
set -e

# Legacy path: earlier versions ran HAL in tmux. Leaving that copy alive would
# hold port 5001 and the unit would crash-loop on "address already in use",
# which reads like a unit bug rather than a leftover session.
tmux kill-session -t $SERVICE 2>/dev/null && echo "[spike-hal] killed the old tmux session" || true
sudo pkill -f 'uvicorn hal.server:app' 2>/dev/null || true
sleep 2

# HAL runs as root, exactly like the production unit. It is not a preference:
# config.py hardcodes a dozen state paths under /root (users, strangers,
# face/pose models, agent workspaces, OS_CONFIG_PATH), and only the first of
# them surfaces as a crash — overriding them one env var at a time is a game of
# whack-a-mole that diverges from how the device really runs.
# Footprint outside our tree: /root/local/** and /var/log/hal (uninstall below).
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<'UNIT'
[Unit]
Description=HAL Hardware Runtime (Autonomous, spike layout)
# The Pollen daemon owns the camera and both ALSA PCMs and must be up before HAL
# asks for them; it is also where the motion driver connects (localhost:8000).
After=network.target reachy-mini-daemon.service
Wants=reachy-mini-daemon.service

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_BASE/hal
# DEVICE_TYPE and DEVICES_DIR live in this file, not in Environment= below:
# systemd applies EnvironmentFile= after Environment=, so anything named in both
# takes the file's value. Step 2 of this script is what makes DEVICES_DIR point
# at the deployed tree.
EnvironmentFile=$REMOTE_BASE/hal/.env
# Safe as an Environment= line only because the .env does not set HF_HOME — if
# it ever does, the file wins and this becomes dead weight.
Environment=HF_HOME=$HF_HOME_PATH
# Borrow the camera and microphone from the daemon before HAL opens them. On a
# cold boot the daemon's HTTP port may not be listening yet, hence the retry.
# TODO: this belongs in HAL's own lifespan (with acquire on shutdown) — it lives
# here only until that code exists.
ExecStartPre=/bin/sh -c 'for i in 1 2 3 4 5; do curl -sf -X POST $DAEMON/api/media/release >/dev/null && exit 0; sleep 2; done; echo "media release failed — camera/audio will report busy"; exit 0'
# --timeout-graceful-shutdown: without it uvicorn waits forever for open
# connections (an SSE/MJPEG stream holds SIGTERM until systemd's 90s SIGKILL).
ExecStart=$REMOTE_BASE/hal/.venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001 --timeout-graceful-shutdown 5
# Give the media back so Pollen's own stack is not left deaf and blind. Runs on
# every stop, including a restart — where ExecStartPre takes it straight back.
ExecStopPost=-/usr/bin/curl -sf -m 5 -X POST $DAEMON/api/media/acquire
TimeoutStopSec=30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null 2>&1
sudo systemctl restart $SERVICE

# HAL boots slowly on a CM4 — torch and the model stack dominate.
echo -n "[spike-hal] waiting for HAL "
for i in \$(seq 1 60); do curl -sf localhost:5001/health >/dev/null 2>&1 && break; printf .; sleep 2; done; echo
echo "--- health ---"; curl -s localhost:5001/health; echo
echo "--- device ---"; curl -s localhost:5001/device; echo
REMOTE_START

cat <<EOF

========================================
  HAL running under systemd on $REACHY_HOST
    logs     : ssh $REACHY_HOST 'journalctl -u $SERVICE -f'
    health   : curl ${REACHY_HOST#*@}:5001/health
    routes   : curl ${REACHY_HOST#*@}:5001/device
    restart  : ssh $REACHY_HOST 'sudo systemctl restart $SERVICE'
    redeploy : bash devices/reachy-mini/spike-hal.sh --no-deps
    stop     : bash devices/reachy-mini/spike-hal.sh --stop   <- also returns media
========================================

It now comes back on its own after a reboot. The daemon's camera and audio stay
on loan to HAL for as long as the unit runs.

Full uninstall (leaves Pollen OS as it was):
  bash devices/reachy-mini/spike-hal.sh --uninstall
  ssh $REACHY_HOST 'sudo rm -rf $REMOTE_BASE /root/local /var/log/hal && sudo rm -f /etc/asound.conf'
  # /root/local is HAL state (users, strangers, models) — it runs as root here,
  # same as the production systemd unit, so it writes outside our tree.
  # if a backup exists: sudo mv /etc/asound.conf.pre-autonomous /etc/asound.conf
EOF
