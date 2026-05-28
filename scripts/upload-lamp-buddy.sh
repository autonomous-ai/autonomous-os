#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUDDY_DIR="${ROOT_DIR}/lamp-buddy"
VERSION_FILE="${BUDDY_DIR}/VERSION_LAMP_BUDDY"
DIST_DIR="${BUDDY_DIR}/dist"

# Bucket and path: lamp/ota/lamp-buddy/[semver].dmg
GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"

# Build target — `dmg` (unsigned, default) or `dmg-signed` (Developer ID + notarized).
# Override via env: BUDDY_DMG_TARGET=dmg-signed scripts/upload-lamp-buddy.sh
DMG_TARGET="${BUDDY_DMG_TARGET:-dmg}"

# Auto-increment semver (patch) before build
if [[ -f "$VERSION_FILE" ]]; then
  current_version=$(tr -d '[:space:]' < "$VERSION_FILE")
  IFS='.' read -r major minor patch <<< "$current_version"
  patch=$((patch + 1))
  new_version="${major}.${minor}.${patch}"
  echo "$new_version" > "$VERSION_FILE"
  echo "========== Version bumped: ${current_version} -> ${new_version} =========="
else
  echo "1.0.0" > "$VERSION_FILE"
  new_version="1.0.0"
  echo "========== Version initialized: ${new_version} =========="
fi

DMG_NAME="LampBuddy-${new_version}.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"
GCS_PATH="${GCS_PATH:-lamp/ota/lamp-buddy/${new_version}.dmg}"

echo "========== Building DMG via 'make ${DMG_TARGET}' (VERSION=${new_version}) =========="
(cd "$BUDDY_DIR" && make "$DMG_TARGET")

if [[ ! -f "$DMG_PATH" ]]; then
  echo "Error: expected DMG not found at $DMG_PATH"
  exit 1
fi

echo "========== Upload ${DMG_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/x-apple-diskimage" \
       cp "$DMG_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (lamp/ota/metadata.json) - lamp-buddy key
METADATA_PATH="lamp/ota/metadata.json"
METADATA_TMP=$(mktemp)
BUDDY_URL="${BUDDY_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

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
data.pop('lumi-buddy', None)
data['lamp-buddy'] = {'version': sys.argv[1], 'url': sys.argv[2]}
print(json.dumps(data, indent=2))
" "$new_version" "$BUDDY_URL")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (lamp-buddy: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
echo "URL:  ${BUDDY_URL}"
