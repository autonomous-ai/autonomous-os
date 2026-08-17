#!/usr/bin/env bash
# install.sh — one-command bring-up of Autonomous OS on a Reachy Mini.
#
# SSH into your robot, then:
#
#   curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/devices/reachy-mini/install.sh | sudo bash
#
# That is all a Reachy Mini needs. No repo to clone, no Go or Node toolchain, no
# build step, nothing copied from your laptop.
#
# What it does: fetches the released reachy-mini device package from the OTA
# feed — which ships the spike scripts along with ROBOT.md and the rootfs
# overlay — and hands over to spike.sh, which installs HAL, os-server, the web
# UI, the agent gateway and the OTA worker, each behind a systemd unit.
#
# Why the scripts come from OTA rather than from this repo: it keeps the whole
# install on ONE released snapshot. Pulling scripts from git main while pulling
# binaries from the OTA feed is how you end up running an installer that expects
# a build nobody has published yet. This file is the only piece fetched from
# git, and it is deliberately small enough to read in full before you pipe it to
# a shell.
#
# It installs ALONGSIDE the Pollen software that shipped with the robot; it does
# not replace it. The Pollen daemon keeps running and keeps owning motion. HAL
# borrows the camera and microphone from it and hands them back on shutdown.
#
# Options (environment):
#   OTA_METADATA_URL=…   install from another feed (staging, a fork)
#   DEVICE_TYPE=…        another device profile (default: reachy-mini)
#
# Anything after `bash -s --` is passed to spike.sh, e.g.:
#   curl -fsSL …/install.sh | sudo bash -s -- --skip agent
#   curl -fsSL …/install.sh | sudo bash -s -- --uninstall
set -euo pipefail

DEVICE_TYPE="${DEVICE_TYPE:-reachy-mini}"
OTA_METADATA_URL="${OTA_METADATA_URL:-https://cdn.autonomous.ai/os/ota/metadata.json}"

tag() { printf '[install] %s\n' "$1" >&2; }
fail() { printf '[install] ERROR: %s\n' "$1" >&2; exit 1; }

# --force is ours, not spike.sh's, so filter it out of the argument list rather
# than rewriting it in place: substituting it for an empty string would leave a
# blank argument that spike.sh rejects as an unknown flag.
FORCE=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--force" ]; then FORCE=1; else ARGS+=("$a"); fi
done
set -- ${ARGS[@]+"${ARGS[@]}"}

[ "$(id -u)" -eq 0 ] || fail "run as root — pipe to 'sudo bash', not 'bash'"

# Refuse to run anywhere but a Reachy Mini. A one-liner published in a README
# gets pasted into the wrong terminal eventually, and this one is not harmless
# there: it apt-installs packages, overwrites /etc/asound.conf for the whole
# machine, writes /opt and /root/config, and enables five systemd units. On a
# laptop it merely fails; on some other Linux box it succeeds and leaves a mess.
#
# The marker is the Pollen daemon, since that is what the install actually
# depends on: motion goes through it, and HAL borrows the camera and microphone
# from it. Its systemd unit is checked as well as its port, so the guard still
# passes while the daemon happens to be restarting.
if [ "$FORCE" = "0" ]; then
  if systemctl list-unit-files reachy-mini-daemon.service >/dev/null 2>&1 \
     && systemctl cat reachy-mini-daemon.service >/dev/null 2>&1; then
    :
  elif curl -sf -m 3 http://localhost:8000/api/media/status >/dev/null 2>&1; then
    :
  else
    fail "this does not look like a Reachy Mini.
No reachy-mini-daemon.service, and nothing answering on localhost:8000.

Run it ON the robot, over SSH — not on your laptop:
    ssh pollen@reachy-mini.local
    curl -fsSL …/install.sh | sudo bash

If you really mean to install here, re-run with --force."
  fi
fi

tag "device : $DEVICE_TYPE"
tag "feed   : $OTA_METADATA_URL"

# jq is not on the shipped Pollen OS; curl and unzip are. Install what is
# missing before anything depends on it.
missing=()
for t in curl unzip jq; do command -v "$t" >/dev/null || missing+=("$t"); done
if [ ${#missing[@]} -gt 0 ]; then
  tag "installing: ${missing[*]}"
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >&2 \
    || fail "could not install ${missing[*]}"
fi

tag "reading the OTA feed"
META="$(mktemp)"
trap 'rm -f "$META"' EXIT
# no-cache: the CDN edge-caches these, and a stale read installs yesterday's
# build while reporting today's version.
curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o "$META" "$OTA_METADATA_URL" \
  || fail "could not fetch $OTA_METADATA_URL"
# A captive portal answers 200 with HTML. Catch that here rather than let jq
# return empty strings that turn into a confusing "no url for device".
jq -e . "$META" >/dev/null 2>&1 || fail "$OTA_METADATA_URL did not return JSON (captive portal?)"

PKG_URL="$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].url // empty' "$META")"
PKG_VER="$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].version // empty' "$META")"
[ -n "$PKG_URL" ] || fail "the feed has no device profile for '$DEVICE_TYPE'
Published profiles: $(jq -r '.devices | keys | join(", ")' "$META")"

tag "device package $DEVICE_TYPE $PKG_VER"
STAGING="$(mktemp -d)"
trap 'rm -f "$META"; rm -rf "$STAGING"' EXIT
curl -fsSL -H 'Cache-Control: no-cache' -o "$STAGING/pkg.zip" "$PKG_URL" \
  || fail "could not download the device package from $PKG_URL"
unzip -o -q "$STAGING/pkg.zip" -d "$STAGING" || fail "the device package is not a readable zip"

# Two generations of this package exist. The older one ships a spike.sh that
# runs FROM A MAC (ssh + rsync + local Go/Node builds) — running it here would
# fail in confusing ways. spike-lib.sh only exists in the run-on-the-robot
# generation, so it is the marker to gate on, not spike.sh.
if [ ! -f "$STAGING/spike-lib.sh" ]; then
  [ -f "$STAGING/spike.sh" ] \
    && fail "device package $PKG_VER ships the older Mac-side scripts, which cannot run here.
Publish a current one:  make upload-device $DEVICE_TYPE" \
    || fail "device package $PKG_VER predates the installer scripts.
Publish a current one:  make upload-device $DEVICE_TYPE"
fi

tag "handing over to spike.sh"
echo >&2
# Pass the feed down so every step installs from the same place this script
# read, even when the robot's bootstrap.json points somewhere else.
OTA_METADATA_URL="$OTA_METADATA_URL" DEVICE_TYPE="$DEVICE_TYPE" \
  bash "$STAGING/spike.sh" "$@"

# The staging copy is temporary, but spike.sh's first step installs the same
# package to /opt/devices — so the scripts survive there for later use. Say so:
# otherwise the only way anyone knows how to undo this is to run the installer
# again, and that reinstalls instead.
cat >&2 <<EOF

[install] The scripts now live at /opt/devices/$DEVICE_TYPE/ — use them to
[install] stop or remove the stack later:
[install]     sudo bash /opt/devices/$DEVICE_TYPE/spike.sh --stop
[install]     sudo bash /opt/devices/$DEVICE_TYPE/spike.sh --uninstall
EOF
