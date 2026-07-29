#!/usr/bin/env bash
# spike-reachy.sh — Quick dev deploy of Autonomous OS onto a Reachy Mini.
#
# Runs FROM YOUR MAC. Builds locally, scp's to the robot's Pi, installs deps,
# and starts services in a tmux session. No OTA, no golden image, no systemd.
#
# Prerequisites:
#   - SSH access to the Reachy Mini's Pi (key-based or password)
#   - the login must have passwordless sudo (Pollen's `pollen` user does; root
#     cannot SSH in directly on the shipped OS)
#   - Pollen daemon running on the Pi (localhost:8000)
#   - tmux installed on the Pi (apt install tmux)
#
# Usage:
#   REACHY_HOST=pollen@reachy-mini.local bash devices/reachy-mini/spike.sh
#
# To tear down:
#   ssh $REACHY_HOST "tmux kill-session -t autonomous"
set -euo pipefail

REACHY_HOST="${REACHY_HOST:?Set REACHY_HOST=user@ip}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_BASE="/opt/autonomous"

echo "========== 1/5  Build os-server (linux/arm64) =========="
cd "$ROOT_DIR"
make os-build

echo "========== 2/5  Build web UI =========="
make web-build

echo "========== 3/5  Copy files to $REACHY_HOST =========="
# /opt is root-owned on Pollen OS, and the SSH login is an unprivileged user
# (`pollen`). Create the tree with sudo, then hand it to the login user so the
# scp/rsync steps below — and `uv sync` later — do not need sudo at all.
ssh "$REACHY_HOST" "sudo mkdir -p $REMOTE_BASE/{hal,devices,web,config} && sudo chown -R \$(id -u):\$(id -g) $REMOTE_BASE"

# os-server binary
scp system/os-server "$REACHY_HOST:$REMOTE_BASE/os-server"
ssh "$REACHY_HOST" "chmod +x $REMOTE_BASE/os-server"

# HAL — exclude heavy/local-only dirs
rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '*.pyc' --exclude 'test/' --exclude '.env' \
  "$ROOT_DIR/hal/" "$REACHY_HOST:$REMOTE_BASE/hal/"

# Device profile
rsync -az --delete "$ROOT_DIR/devices/reachy-mini/" "$REACHY_HOST:$REMOTE_BASE/devices/reachy-mini/"

# Rootfs overlay (device .env) — only if .env doesn't exist yet (don't clobber tuned values)
ssh "$REACHY_HOST" "[ -f $REMOTE_BASE/hal/.env ] || cp $REMOTE_BASE/devices/reachy-mini/rootfs/opt/hal/.env $REMOTE_BASE/hal/.env"

# Web dist
rsync -az --delete "$ROOT_DIR/system/web/dist/" "$REACHY_HOST:$REMOTE_BASE/web/"

echo "========== 4/5  Install deps on Pi =========="
# Ensure uv is available, install HAL Python deps.
# pygobject/pycairo (reachy extra) need cairo dev libs — install if missing.
ssh "$REACHY_HOST" bash <<'REMOTE_DEPS'
set -e
export PATH="$HOME/.local/bin:$PATH"

# uv
if ! command -v uv &>/dev/null; then
  echo "[spike] Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# tmux
command -v tmux &>/dev/null || { echo "[spike] Installing tmux..."; sudo apt-get update && sudo apt-get install -y tmux; }

# System libs for pygobject/pycairo (reachy SDK transitive deps).
# Safe to run even if already installed — Pollen OS ships them, but a fresh
# image or a different unit may not.
sudo apt-get install -y --no-install-recommends \
  libcairo2-dev libgirepository1.0-dev pkg-config 2>/dev/null || \
  echo "[spike] WARN: apt install failed — pygobject may fail to build"

# HAL venv
cd /opt/autonomous/hal
uv sync --python 3.12 --extra hardware --extra reachy
REMOTE_DEPS

echo "========== 5/5  Start services in tmux =========="
ssh "$REACHY_HOST" bash <<'REMOTE_START'
set -e
export PATH="$HOME/.local/bin:$PATH"
BASE="/opt/autonomous"

# Kill previous session if exists
tmux kill-session -t autonomous 2>/dev/null || true

# Create tmux session with 3 panes
tmux new-session -d -s autonomous -n services

# Pane 0: HAL
tmux send-keys -t autonomous:services.0 \
  "cd $BASE/hal && DEVICE_TYPE=reachy-mini DEVICES_DIR=$BASE/devices .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port 5001 2>&1 | tee /tmp/hal.log" Enter

tmux split-window -t autonomous:services -v

# Pane 1: os-server
tmux send-keys -t autonomous:services.1 \
  "cd $BASE && DEVICE_TYPE=reachy-mini DEVICES_DIR=$BASE/devices ./os-server 2>&1 | tee /tmp/os-server.log" Enter

tmux split-window -t autonomous:services -v

# Pane 2: status
# os-server binds 127.0.0.1:5000 (see system/server/server.go) — it is reachable
# only from the Pi itself until nginx fronts it, so this probe must run here.
tmux send-keys -t autonomous:services.2 \
  "echo 'Waiting for services...'; sleep 3; echo '--- HAL ---'; curl -s localhost:5001/health || echo 'HAL not ready'; echo '--- OS ---'; curl -s localhost:5000/api/health || echo 'os-server not ready'; echo; echo 'tmux attach -t autonomous'" Enter

echo ""
echo "========================================"
echo "  Services started in tmux session."
echo "  SSH in and run: tmux attach -t autonomous"
echo "  Verify HAL from anywhere: curl <IP>:5001/health"
echo "  Verify os-server ON THE PI:  curl localhost:5000/api/health"
echo "========================================"
REMOTE_START

echo "Done. SSH into $REACHY_HOST and run: tmux attach -t autonomous"
