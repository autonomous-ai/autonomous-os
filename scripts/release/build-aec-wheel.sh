#!/usr/bin/env bash
# Build the aarch64 wheel for `aec-audio-processing` ON a device, then fetch it.
#
# PyPI ships Windows wheels only, so aarch64 has to compile the vendored
# webrtc-audio-processing + abseil. Doing that on the target device is the
# simplest way to get the right ABI (same Debian, same uv-managed CPython 3.12)
# without cross-compilation or a container.
#
# meson and ninja are installed FROM PyPI into a throwaway venv, so this needs
# no apt and no root-owned system change: everything lives under /tmp on the
# device and is removed afterwards. /opt/hal is never touched.
#
# Usage:
#   scripts/release/build-aec-wheel.sh <device-ip> [version]
#   make upload-aec-wheel        # publishes what this produced
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

DEVICE_IP="${1:-}"
VERSION="${2:-1.0.1}"
DEVICE_USER="${DEVICE_USER:-orangepi}"
DEVICE_PASS="${DEVICE_PASS:-orangepi}"
OUT_DIR="${WHEEL_DIR:-${ROOT_DIR}/dist/aec}"
REMOTE_WORK="/tmp/aec-wheel-build"

if [[ -z "$DEVICE_IP" ]]; then
  echo "Usage: $0 <device-ip> [version]"
  exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "Error: sshpass not found — brew install hudochenkov/sshpass/sshpass"
  exit 1
fi

SSH=(sshpass -p "$DEVICE_PASS" ssh -o StrictHostKeyChecking=no "${DEVICE_USER}@${DEVICE_IP}")
SCP=(sshpass -p "$DEVICE_PASS" scp -o StrictHostKeyChecking=no)

echo "========== Building ${VERSION} on ${DEVICE_IP} =========="

# Run as root: uv lives in /root/.local/bin and the venv it creates is root-owned.
"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S bash -s" <<REMOTE
set -euo pipefail
UV=/root/.local/bin/uv
[ -x "\$UV" ] || { echo "uv not found at \$UV"; exit 1; }

rm -rf ${REMOTE_WORK}
mkdir -p ${REMOTE_WORK}
cd ${REMOTE_WORK}
export UV_LINK_MODE=copy

# meson/ninja from PyPI — deliberately NOT apt, so the device keeps the exact
# system packages the image shipped with.
"\$UV" venv --python 3.12 .venv
"\$UV" pip install --python ${REMOTE_WORK}/.venv/bin/python pip meson ninja setuptools wheel
export PATH="${REMOTE_WORK}/.venv/bin:\$PATH"

# --no-binary names the package explicitly, NOT ":all:". With ":all:" pip also
# refuses wheels for the BUILD dependencies (swig, meson, ninja, cmake) and
# bootstraps cmake from source on the board — hours of compile for nothing.
${REMOTE_WORK}/.venv/bin/python -m pip wheel --no-binary aec-audio-processing --no-deps \
  --wheel-dir ${REMOTE_WORK}/dist "aec-audio-processing==${VERSION}"

ls -la ${REMOTE_WORK}/dist
REMOTE

mkdir -p "$OUT_DIR"
echo "========== Fetching wheel → ${OUT_DIR} =========="
"${SCP[@]}" "${DEVICE_USER}@${DEVICE_IP}:${REMOTE_WORK}/dist/aec_audio_processing-*.whl" "$OUT_DIR/"

echo "========== Verifying the wheel installs (clean venv on device) =========="
"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S bash -s" <<REMOTE
set -euo pipefail
UV=/root/.local/bin/uv
export UV_LINK_MODE=copy
rm -rf ${REMOTE_WORK}/verify
"\$UV" venv --python 3.12 ${REMOTE_WORK}/verify
"\$UV" pip install --python ${REMOTE_WORK}/verify/bin/python ${REMOTE_WORK}/dist/aec_audio_processing-*.whl
${REMOTE_WORK}/verify/bin/python -c "from aec_audio_processing import AudioProcessor; print('import OK:', AudioProcessor)"
REMOTE

echo "========== Cleaning up the device =========="
"${SSH[@]}" "echo ${DEVICE_PASS} | sudo -S rm -rf ${REMOTE_WORK}"

echo
ls -la "$OUT_DIR"
echo
echo "Next: make upload-aec-wheel"
