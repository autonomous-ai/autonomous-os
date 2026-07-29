#!/usr/bin/env bash
# spike-device.sh — install the Reachy Mini device profile from OTA.
#
# RUNS ON THE ROBOT. Pulls devices.<type> from the OTA metadata into
# /opt/devices/<type> and applies that package's rootfs/ overlay onto /.
#
# Run this FIRST. Everything else depends on it:
#   DEVICE.md          HAL refuses to boot without it, and the board list is
#                      what lets it accept a CM4 at all
#   rootfs/etc/asound.conf   defines the shared dmix/dsnoop PCMs and the ALSA
#                      default; without it HAL has no output device and every
#                      TTS call dies on PortAudio device -1
#   rootfs/opt/hal/.env      the device's HAL settings
#
# Nothing here is copied from a developer machine — the package is the same one
# the imager and scripts/provision/setup.sh install, so a spike robot runs what
# the fleet runs.
#
# Usage:
#   sudo bash spike-device.sh              # install / update the profile
#   sudo bash spike-device.sh --keep-env   # do not touch an existing /opt/hal/.env
#   sudo bash spike-device.sh --uninstall  # remove the profile (leaves rootfs files)
set -euo pipefail

SPIKE_TAG="spike-device"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

KEEP_ENV=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --keep-env)  KEEP_ENV=1 ;;
    --uninstall) UNINSTALL=1 ;;
    # Accepted and ignored: the profile is files on disk, not a service, so
    # there is nothing to stop. spike.sh passes --stop to every step, and
    # rejecting it here would make a clean teardown print a failure.
    --stop)      info "nothing to stop — the device profile is not a service"; exit 0 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$UNINSTALL" = "1" ]; then
  say "Removing the device profile"
  rm -rf "${DEVICES_DIR:?}/$DEVICE_TYPE"
  info "removed $DEVICES_DIR/$DEVICE_TYPE"
  info "left in place: /etc/asound.conf and $HAL_DIR/.env — other units still read them"
  exit 0
fi

say "1/3  Fetch the device profile from OTA"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
ota_unpack device "$STAGING"

[ -f "$STAGING/DEVICE.md" ] || die "the $DEVICE_TYPE package has no DEVICE.md"

say "2/3  Install to $DEVICES_DIR/$DEVICE_TYPE"
mkdir -p "$DEVICES_DIR/$DEVICE_TYPE"
# Mirror rather than merge: a capability or a rootfs file removed upstream must
# disappear here too, or HAL keeps mounting routes the device no longer claims.
# The rootfs/ subtree is copied on below, so deleting it here costs nothing.
rm -rf "${DEVICES_DIR:?}/$DEVICE_TYPE"
mkdir -p "$DEVICES_DIR/$DEVICE_TYPE"
cp -a "$STAGING/." "$DEVICES_DIR/$DEVICE_TYPE/"
info "profile version: $(cat "$DEVICES_DIR/$DEVICE_TYPE/VERSION" 2>/dev/null || echo unknown)"

say "3/3  Apply the rootfs overlay onto /"
OVERLAY="$DEVICES_DIR/$DEVICE_TYPE/rootfs"
if [ ! -d "$OVERLAY" ]; then
  # Older packages shipped only the contract files. Say so loudly: without the
  # overlay there is no /etc/asound.conf, and the audio failure it causes shows
  # up much later as "TTS playback setup failed ... device -1".
  info "WARN: this package has no rootfs/ — audio config will be missing."
  # Positional, not a variable: the Makefile target takes the device type as an
  # argument (make upload-device lamp), so DEVICE_TYPE=... silently publishes
  # nothing useful.
  info "WARN: publish a newer device package (make upload-device $DEVICE_TYPE)."
  exit 0
fi

# Back up anything of Pollen's we are about to replace, once. Their OS ships no
# /etc/asound.conf today, so this normally creates rather than overwrites — but
# a future Pollen image may add one, and this script must never be the reason
# their config disappeared without a copy.
while IFS= read -r rel; do
  target="/$rel"
  src="$OVERLAY/$rel"
  if [ -f "$target" ] && ! cmp -s "$target" "$src" && [ ! -f "$target.pre-autonomous" ]; then
    cp -a "$target" "$target.pre-autonomous"
    info "backed up $target -> $target.pre-autonomous"
  fi
done < <(cd "$OVERLAY" && find . -type f -printf '%P\n')

# .env carries values an operator may have tuned on the robot. Keep theirs
# unless asked, but still install it when absent — HAL will not start without
# DEVICE_TYPE.
ENV_SRC="$OVERLAY/opt/hal/.env"
ENV_DST="$HAL_DIR/.env"
PRESERVE_ENV=0
if [ -f "$ENV_DST" ] && [ "$KEEP_ENV" = "1" ]; then
  PRESERVE_ENV=1
  ENV_BACKUP="$(mktemp)"
  cp -a "$ENV_DST" "$ENV_BACKUP"
fi

mkdir -p "$HAL_DIR"
cp -a "$OVERLAY/." /

if [ "$PRESERVE_ENV" = "1" ]; then
  cp -a "$ENV_BACKUP" "$ENV_DST"
  rm -f "$ENV_BACKUP"
  info "--keep-env: kept the existing $ENV_DST"
elif [ -f "$ENV_SRC" ]; then
  info "installed $ENV_DST from the package"
fi

info "overlay applied:"
(cd "$OVERLAY" && find . -type f -printf '  /%P\n')

cat <<EOF

========================================
  Device profile installed
    profile : $DEVICES_DIR/$DEVICE_TYPE
    board   : $(grep -oE 'boards:.*' "$DEVICES_DIR/$DEVICE_TYPE/DEVICE.md" 2>/dev/null || echo '?')
    next    : sudo bash spike-hal.sh
========================================
EOF
