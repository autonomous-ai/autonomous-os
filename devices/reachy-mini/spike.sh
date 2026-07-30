#!/usr/bin/env bash
# spike.sh — bring the whole Autonomous stack up on a Reachy Mini, in one run.
#
# RUNS ON THE ROBOT. Copy this folder over, then:
#
#   scp -r devices/reachy-mini pollen@reachy-mini.local:~/
#   ssh pollen@reachy-mini.local 'sudo bash ~/reachy-mini/spike.sh'
#
# Nothing is built here and nothing is copied from a developer machine: every
# component comes from the OTA metadata feed, the same source the imager and
# scripts/provision/setup.sh read. So a spike robot runs what the fleet runs,
# and the only thing you need locally is this folder.
#
# This installs onto the Pollen OS that shipped with the robot — it does not
# replace it. The Pollen daemon keeps running and keeps owning motion; HAL
# borrows the camera and microphone from it and hands them back on shutdown.
#
# It is a thin orchestrator ON PURPOSE. The previous version of this file
# duplicated what the per-component scripts did, drifted from them within a
# week, and ended up running os-server against the wrong config directory while
# every service still reported healthy. Nothing below is reimplemented here — if
# a step is wrong, it is wrong in that step's own script, for everyone.
#
# Order matters:
#   device     DEVICE.md, /etc/asound.conf and /opt/hal/.env — everything else
#              reads them, and HAL will not boot without the first
#   hal         the body: motion, camera, audio, sensing
#   os-server   the API and agent gateway (reads the same /root/config)
#   web         nginx in front of both
#   agent       the OpenClaw runtime os-server drives
#   bootstrap   LAST — it can restart os-server and hal the moment it finds a
#               newer build, and doing that mid-install turns a clean bring-up
#               into a race
#
# Usage:
#   sudo bash spike.sh                  # full bring-up
#   sudo bash spike.sh --no-deps        # skip HAL's uv sync (fast re-run)
#   sudo bash spike.sh --skip agent     # skip one or more steps (repeatable)
#   sudo bash spike.sh --stop           # stop everything, reverse order
#   sudo bash spike.sh --uninstall      # stop + remove units and artifacts
#
#   sudo OTA_METADATA_URL=https://…/metadata.json bash spike.sh   # another feed
set -euo pipefail

SPIKE_TAG="spike"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=spike-lib.sh
. "$HERE/spike-lib.sh"

STEPS=(device hal os web agent bootstrap)

PASS_ARGS=()
SKIP=()
MODE="install"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-deps)   PASS_ARGS+=("--no-deps") ;;
    --skip)      shift; [ $# -gt 0 ] || die "--skip needs a step name"; SKIP+=("$1") ;;
    --stop)      MODE="stop" ;;
    --uninstall) MODE="uninstall" ;;
    *) die "unknown flag: $1 (steps: ${STEPS[*]})" ;;
  esac
  shift
done

ensure_root "$@"

script_for() { echo "$HERE/spike-$1.sh"; }

skipped() {
  local s
  for s in ${SKIP[@]+"${SKIP[@]}"}; do [ "$s" = "$1" ] && return 0; done
  return 1
}

# --- teardown ----------------------------------------------------------------
# Reverse order: bootstrap first so it cannot reinstall something a later step
# is still tearing down.
if [ "$MODE" != "install" ]; then
  flag="--stop"; [ "$MODE" = "uninstall" ] && flag="--uninstall"
  say "Tearing down (${MODE})"
  for (( i=${#STEPS[@]}-1 ; i>=0 ; i-- )); do
    step="${STEPS[$i]}"
    s="$(script_for "$step")"
    [ -f "$s" ] || continue
    # Keep going on failure: a half-installed robot must still be cleanable.
    bash "$s" "$flag" || info "WARN: $step $flag failed — continuing"
  done
  echo
  info "done. Pollen OS is untouched apart from what each script's header lists."
  exit 0
fi

# --- install -----------------------------------------------------------------
say "Reachy Mini — full spike"
ensure_tools
# One snapshot of the feed for the whole run: cleared here so the run is fresh,
# then shared by all six steps so a publish landing mid-install cannot leave
# os-server and hal on mismatched builds.
clear_metadata_cache
info "OTA feed : $(metadata_url)"
info "device   : $DEVICE_TYPE"
info "steps    : ${STEPS[*]}"
[ ${#SKIP[@]} -eq 0 ] || info "skipping : ${SKIP[*]}"

# Fail loudly and immediately rather than half-installing: a robot left with
# HAL but no os-server looks alive and does nothing useful.
for step in "${STEPS[@]}"; do
  [ -f "$(script_for "$step")" ] || die "missing $(script_for "$step") — copy the whole folder over"
done

START_TS=$SECONDS
for step in "${STEPS[@]}"; do
  if skipped "$step"; then
    info "skip $step"
    continue
  fi
  say "STEP: $step"
  # --no-deps is only meaningful to HAL; passing it to the others would abort
  # on an unknown flag.
  if [ "$step" = "hal" ]; then
    bash "$(script_for "$step")" ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}
  else
    bash "$(script_for "$step")"
  fi
done

say "Bring-up complete in $(( (SECONDS - START_TS) / 60 ))m $(( (SECONDS - START_TS) % 60 ))s"
# To stderr, like say/info. Mixing the two streams is not cosmetic here: when
# the run is piped to a file or a log, stderr is unbuffered while stdout is
# block-buffered, so the labels and their values arrive out of order and the
# summary comes out shuffled — "hal : " on one line and "up" three lines later.
{
  printf 'hal       : '; curl -sf -m 5 localhost:5001/health >/dev/null 2>&1 && echo up || echo DOWN
  printf 'os-server : '; curl -sf -m 5 localhost:5000/api/health/live >/dev/null 2>&1 && echo up || echo DOWN
  printf 'web       : '; curl -sf -m 5 localhost/ >/dev/null 2>&1 && echo up || echo DOWN
  printf 'media     : '; curl -s -m 5 "$DAEMON_URL/api/media/status" 2>/dev/null || echo '?'; echo
  printf 'motors    : '; curl -s -m 5 "$DAEMON_URL/api/motors/status" 2>/dev/null || echo '?'; echo
} >&2

# Resolved here, not left as an escaped $( ) inside the heredoc — that printed
# the command text at the operator instead of the address they need to open.
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat >&2 <<EOF

========================================
  Open http://${IP:-<robot-ip>}/ in a browser to finish setup.

  logs   : journalctl -u hal -u os-server -u openclaw -u bootstrap -f
  stop   : sudo bash spike.sh --stop
  remove : sudo bash spike.sh --uninstall
========================================

Everything survives a reboot — each step installs a systemd unit.
EOF
