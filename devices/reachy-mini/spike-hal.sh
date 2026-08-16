#!/usr/bin/env bash
# spike-hal.sh — install the HAL hardware runtime on a Reachy Mini.
#
# RUNS ON THE ROBOT. Pulls the `hal` component from OTA metadata into /opt/hal,
# builds its Python venv, and runs uvicorn under systemd.
#
# This used to rsync hal/ straight out of a developer's working tree. That
# shipped whatever was checked out on someone's laptop rather than what the
# fleet runs, so anything reproduced here said nothing about anyone else's
# build. Everything now comes from the OTA feed, like the imager and
# scripts/provision/setup.sh.
#
# Run spike-device.sh FIRST. It installs ROBOT.md (HAL refuses to boot without
# one, and its board list is what lets HAL accept a CM4), /opt/hal/.env, and
# /etc/asound.conf — which defines the shared ALSA PCMs and the default device.
# Without that file PortAudio has no output device at all and every TTS call
# fails with "Error querying device -1", while aplay from a shell still works.
#
# Media: HAL borrows the camera and both ALSA PCMs from the Pollen daemon by
# itself, because ROBOT.md declares `owner: pollen_daemon` on audio and vision.
# It releases at the top of startup and hands them back on shutdown. Nothing
# here has to call /api/media/* — and nothing should: the release has to be
# ordered against HAL's own audio probe, which only code inside the process can
# guarantee.
#
# Usage:
#   sudo bash spike-hal.sh              # install + start
#   sudo bash spike-hal.sh --no-deps    # skip uv sync (fast redeploy)
#   sudo bash spike-hal.sh --stop
#   sudo bash spike-hal.sh --uninstall  # stop + remove the unit and /opt/hal
set -euo pipefail

SPIKE_TAG="spike-hal"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="hal"
# HAL runs as root — config.py hardcodes a dozen state paths under /root — so
# HOME is /root and the HuggingFace cache would start empty. Point it at the
# login user's cache instead: the Pollen daemon already downloads the emotes
# move library there, and a second copy of the same models is not free on a
# 14 GB eMMC that ships ~60% full.
HF_HOME_PATH="${HF_HOME_PATH:-/home/pollen/.cache/huggingface}"

SKIP_DEPS=0
STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --no-deps)   SKIP_DEPS=1 ;;
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping HAL"
  stop_unit "$SERVICE"
  # Legacy path: earlier versions of this script ran HAL in tmux.
  tmux kill-session -t hal 2>/dev/null || true
  pkill -f 'uvicorn hal.server:app' 2>/dev/null || true
  sleep 1
  # HAL hands the media back itself on a clean shutdown. This covers a HAL that
  # was killed hard enough to skip it — without it the daemon stays deaf and
  # blind, and so does Pollen's own app stack.
  curl -s -m 5 -X POST "$DAEMON_URL/api/media/acquire" >/dev/null 2>&1 || true
  printf '[%s] media: ' "$SPIKE_TAG"; curl -s -m 5 "$DAEMON_URL/api/media/status" || true; echo
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    rm -rf "${HAL_DIR:?}"
    info "removed the unit and $HAL_DIR"
    info "left behind: /root/local (users, strangers, models) and /var/log/hal"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

[ -f "$DEVICES_DIR/$DEVICE_TYPE/ROBOT.md" ] \
  || die "no $DEVICES_DIR/$DEVICE_TYPE/ROBOT.md — run: sudo bash spike-device.sh"

say "1/4  Preflight: disk space"
# The venv is ~2 GB (torch, opencv, polars, pyarrow) and uv's wheel cache can add
# as much again. The robot ships a 14 GB eMMC that is already ~60% full, and
# filling it hurts the Pollen daemon (journal, state writes) far more than
# anything else this script does. Refuse rather than wedge the robot.
#
# The threshold depends on whether the venv already exists, and getting that
# wrong locks out the update path: a built venv leaves ~1.8 GB free on this eMMC,
# so a flat 4 GB gate means the FIRST install passes and every re-run after it
# dies here — while install.sh is exactly how updates are applied. With a venv
# in place uv only fetches what changed, so the requirement is far smaller.
AVAIL_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
info "free space on /: $(awk -v k="$AVAIL_KB" 'BEGIN {printf "%.1f GB", k/1048576}')"
if [ "$SKIP_DEPS" = "0" ]; then
  if [ -x "$HAL_DIR/.venv/bin/uvicorn" ]; then
    NEED_KB=1048576   # 1 GB — incremental sync over an existing venv
    NEED_LABEL="1 GB (updating an existing venv)"
  else
    NEED_KB=4194304   # 4 GB — download + build from nothing
    NEED_LABEL="4 GB (building the venv from scratch)"
  fi
  if [ "$AVAIL_KB" -lt "$NEED_KB" ]; then
    die "need at least $NEED_LABEL.
Free space first (journalctl --vacuum-size=100M, uv cache clean), or re-run
with --no-deps to skip the sync entirely."
  fi
fi

say "2/4  Install HAL from OTA"
stop_unit "$SERVICE"
pkill -f 'uvicorn hal.server:app' 2>/dev/null || true
# Unzipped over the top rather than into a clean tree: /opt/hal/.env came from
# the device package and must survive, and so must the venv on a redeploy.
mkdir -p "$HAL_DIR"
ota_unpack hal "$HAL_DIR"
[ -f "$HAL_DIR/.env" ] || die "no $HAL_DIR/.env — run spike-device.sh (it ships the overlay)"

if [ "$SKIP_DEPS" = "0" ]; then
  say "3/4  Build the Python venv"
  # pygobject/pycairo build from source for the `reachy` extra and need system
  # headers. Pollen OS ships them, but a fresh image may not.
  apt-get install -y --no-install-recommends \
    libcairo2-dev libgirepository1.0-dev pkg-config >/dev/null 2>&1 \
    || info "WARN: apt install failed — pygobject may fail to build"

  # uv is normally a per-user install; root will not have it on PATH.
  UV="$(command -v uv || true)"
  [ -n "$UV" ] || [ ! -x /home/pollen/.local/bin/uv ] || UV=/home/pollen/.local/bin/uv
  if [ -z "$UV" ]; then
    info "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
    UV=/usr/local/bin/uv
  fi

  cd "$HAL_DIR"
  # Keep the wheel cache inside our tree so `rm -rf /opt/hal` reclaims every
  # byte this script wrote instead of leaving GBs in ~/.cache/uv.
  export UV_CACHE_DIR="$HAL_DIR/.uv-cache"
  # 30s is uv's default per-request timeout — too tight for a 300 MB torch
  # wheel on a shared link, and a single attempt is not a real deploy step.
  export UV_HTTP_TIMEOUT=180
  retry "'$UV' sync --python 3.12 --extra hardware --extra reachy" 3 5 \
    || die "uv sync failed 3 times (network?). The cache is kept — re-run to resume."
  "$UV" cache prune >/dev/null 2>&1 || true
  df -h / | awk 'NR==2 {print "['"$SPIKE_TAG"'] free space after sync: " $4}'
else
  say "3/4  Deps skipped (--no-deps)"
  [ -x "$HAL_DIR/.venv/bin/uvicorn" ] || die "no venv at $HAL_DIR/.venv — drop --no-deps"
fi

say "4/4  Install the unit and start"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=HAL Hardware Runtime
# The Pollen daemon owns the camera and both ALSA PCMs and must be up before HAL
# asks for them; it is also where the motion driver connects (localhost:8000).
After=network.target reachy-mini-daemon.service
Wants=reachy-mini-daemon.service

[Service]
Type=simple
User=root
WorkingDirectory=$HAL_DIR
# DEVICE_TYPE and DEVICES_DIR come from this file. Note systemd applies
# EnvironmentFile AFTER Environment=, so anything named in both takes the
# file's value — set it there, not below.
EnvironmentFile=$HAL_DIR/.env
# Safe as an Environment= line only because the .env does not set HF_HOME.
Environment=HF_HOME=$HF_HOME_PATH
# --timeout-graceful-shutdown: without it uvicorn waits forever for open
# connections (an SSE/MJPEG stream holds SIGTERM until systemd's 90s SIGKILL).
ExecStart=$HAL_DIR/.venv/bin/uvicorn hal.server:app --host 127.0.0.1 --port 5001 --timeout-graceful-shutdown 5
TimeoutStopSec=30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hal

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
# HAL boots slowly on a CM4 — torch and the model stack dominate.
wait_http "http://localhost:5001/health" 120 "HAL" || true
echo "--- health ---"; curl -s -m 5 localhost:5001/health; echo
echo "--- device ---"; curl -s -m 5 localhost:5001/device; echo
printf -- "--- media  --- "; curl -s -m 5 "$DAEMON_URL/api/media/status"; echo

cat <<EOF

========================================
  HAL running under systemd
    version : $(ota_field hal version)
    logs    : journalctl -u $SERVICE -f
    health  : curl localhost:5001/health
    routes  : curl localhost:5001/device
    stop    : sudo bash spike-hal.sh --stop   <- also returns media
========================================

Bound to 127.0.0.1: HAL is reached through os-server's admin proxy
(/api/hardware/*), not directly. For a browser, run spike-web.sh.

Footprint outside $HAL_DIR: /root/local (users, strangers, models), /var/log/hal.
EOF
