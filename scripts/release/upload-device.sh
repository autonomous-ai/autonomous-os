#!/usr/bin/env bash
# Publish ONE device profile as its own OTA artifact.
#
#   upload-device.sh <device-type>     e.g. upload-device.sh lamp
#
# Per-device by design: a team owning a device type publishes only that type and
# only touches its own metadata entry (devices.<type>) — independent teams never
# clobber each other. Namespace-agnostic: everything is built from BUCKET_PREFIX
# (ota-config.sh), so a fork re-namespaces by changing that one knob, not code.
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

DEVICE_TYPE="${1:-}"
if [[ -z "$DEVICE_TYPE" ]]; then
  echo "Usage: upload-device.sh <device-type>   (e.g. lamp, intern, unitree-go2w)" >&2
  exit 1
fi

DEVICE_DIR="${ROOT_DIR}/devices/${DEVICE_TYPE}"
if [[ ! -d "$DEVICE_DIR" ]]; then
  echo "Error: device profile not found at $DEVICE_DIR" >&2
  exit 1
fi
if [[ ! -f "$DEVICE_DIR/DEVICE.md" ]]; then
  echo "Error: $DEVICE_DIR has no DEVICE.md — not a valid device profile" >&2
  exit 1
fi
VERSION_FILE="${DEVICE_DIR}/VERSION"

# Auto-increment semver (patch) before upload.
if [[ -f "$VERSION_FILE" ]]; then
  version=$(cat "$VERSION_FILE" | tr -d '[:space:]')
  IFS='.' read -r major minor patch <<< "$version"
  patch=$((patch + 1))
  new_version="${major}.${minor}.${patch}"
  echo "$new_version" > "$VERSION_FILE"
  echo "========== ${DEVICE_TYPE} version bumped: ${version} -> ${new_version} =========="
else
  echo "1.0.0" > "$VERSION_FILE"
  new_version="1.0.0"
  echo "========== ${DEVICE_TYPE} version initialized: ${new_version} =========="
fi

ZIP_NAME="${DEVICE_TYPE}-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/devices/${DEVICE_TYPE}/${new_version}.zip}"

# Ship the runtime contract (DEVICE.md / SOUL.md / SAFETY.md / VERSION) plus the
# device rootfs overlay (rootfs/ — system config like etc/asound.conf installed
# onto / at build/OTA). Exclude docs/, hardware/ (CAD!), images/ — never read.
echo "========== Zipping devices/${DEVICE_TYPE} (contract + rootfs) to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$DEVICE_DIR" && zip -r "$ZIP_PATH" . \
  -x "docs/*" "hardware/*" "images/*" ".git/*" "*/__pycache__/*" "*.pyc")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json — nested devices.<type> entry. MERGE: never touch other
# device types' entries (independent per-team releases).
METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
DEVICE_URL="${DEVICE_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

echo "========== Fetch metadata from gs://${GCS_BUCKET}/${METADATA_PATH} =========="
if gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP" 2>/dev/null; then
  content=$(cat "$METADATA_TMP")
else
  content=""
fi
if [[ -z "$(echo "$content" | tr -d '[:space:]')" ]]; then
  content="{}"
fi

updated_metadata=$(echo "$content" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    data = {}
devices = data.setdefault('devices', {})
entry = devices.get(sys.argv[1]) if isinstance(devices.get(sys.argv[1]), dict) else {}
entry.update({'version': sys.argv[2], 'url': sys.argv[3], 'updated_at': sys.argv[4]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
devices[sys.argv[1]] = entry
print(json.dumps(data, indent=2))
" "$DEVICE_TYPE" "$new_version" "$DEVICE_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (devices.${DEVICE_TYPE}: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (devices.${DEVICE_TYPE} v${new_version})"
