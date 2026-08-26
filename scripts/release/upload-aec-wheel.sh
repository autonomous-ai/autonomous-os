#!/usr/bin/env bash
# Publish the aarch64 wheel for `aec-audio-processing` (HAL's `aec` extra) as a
# GitHub release asset.
#
# Why a hosted wheel at all: PyPI ships only Windows wheels, so every other
# platform builds the vendored webrtc-audio-processing + abseil from source.
# Measured on a lamp that is 5m35s of device CPU against 2.98s to fetch the
# wheel — and the build needs tools the image does not ship, which an end user
# cannot apt-install on a shipped device.
#
# Why GitHub and not the OTA bucket: this repo is public, the bucket is not.
# A contributor can build a wheel and publish it on their own fork; nobody
# outside the org can write to gs://. The asset is 1.3 MB, well inside what a
# release attachment is meant for.
#
# The tag is per-wheel (`wheels/aec-<version>`), NOT the OS version tag: the
# wheel does not move with OS releases, and a dedicated tag is never re-pointed,
# so a pinned URL cannot silently change content underneath the lockfile.
#
#   scripts/release/build-aec-wheel.sh <device-ip>   # writes dist/aec/*.whl
#   make upload-aec-wheel
#
# The URL it prints is what hal/pyproject.toml pins under [tool.uv.sources].
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

WHEEL_DIR="${WHEEL_DIR:-${ROOT_DIR}/dist/aec}"
REPO="${GITHUB_REPO:-autonomous-ai/autonomous-os}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh not found — brew install gh, then gh auth login"
  exit 1
fi

shopt -s nullglob
wheels=("${WHEEL_DIR}"/aec_audio_processing-*.whl)
shopt -u nullglob

if [[ ${#wheels[@]} -eq 0 ]]; then
  echo "Error: no wheel in ${WHEEL_DIR}"
  echo "Build one first: scripts/release/build-aec-wheel.sh <device-ip>"
  exit 1
fi

for wheel in "${wheels[@]}"; do
  name="$(basename "$wheel")"

  # Refuse anything that is not the device ABI. A cp313 or x86_64 wheel would
  # upload happily and then fail to install on every lamp, at which point the
  # error surfaces on the device instead of here.
  case "$name" in
    *cp312*aarch64*) ;;
    *)
      echo "Error: ${name} is not a cp312/aarch64 wheel — refusing to publish"
      exit 1
      ;;
  esac

  # aec_audio_processing-1.0.1-cp312-... → 1.0.1
  version="$(echo "$name" | cut -d- -f2)"
  tag="wheels/aec-${version}"
  sha256="$(shasum -a 256 "$wheel" | awk '{print $1}')"

  if gh release view "$tag" -R "$REPO" >/dev/null 2>&1; then
    echo "========== Release ${tag} exists — uploading ${name} =========="
    # --clobber so a rebuild of the SAME version replaces the asset. That is a
    # deliberate escape hatch, not the normal path: bumping the wheel should
    # bump the version so the pinned URL changes with it.
    gh release upload "$tag" "$wheel" -R "$REPO" --clobber
  else
    echo "========== Creating release ${tag} =========="
    # --latest=false: this is a dependency artifact, not an OS release, and it
    # must not displace the real "Latest" badge on the releases page.
    gh release create "$tag" "$wheel" -R "$REPO" \
      --title "aec-audio-processing ${version} (aarch64 wheel)" \
      --notes "Prebuilt \`cp312-cp312-linux_aarch64\` wheel for \`aec-audio-processing\` ${version}, used by HAL's \`aec\` extra (see docs/realtime-voice.md).

PyPI publishes Windows wheels only, so aarch64 otherwise compiles the vendored webrtc-audio-processing + abseil — 5m35s on a lamp (A523) against 2.98s to fetch this.

Built with \`scripts/release/build-aec-wheel.sh\` on Debian 12 / aarch64, requires glibc >= 2.34.

sha256: \`${sha256}\`" \
      --latest=false
  fi

  url="https://github.com/${REPO}/releases/download/${tag}/${name}"
  echo
  echo "  url:    ${url}"
  echo "  sha256: ${sha256}"
  echo
  echo "Pin it in hal/pyproject.toml:"
  echo
  echo "  [tool.uv.sources]"
  echo "  aec-audio-processing = { url = \"${url}\" }"
done
