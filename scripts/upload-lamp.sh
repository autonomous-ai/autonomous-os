#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAMP_BIN="${ROOT_DIR}/lamp/lamp-server"
VERSION_FILE="${ROOT_DIR}/lamp/${VERSION_FILE:-VERSION_LAMP}"

# Bucket and path: lamp/ota/lamp/[semver].zip
GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"

# Auto-increment semver (patch) before build
if [[ -f "$VERSION_FILE" ]]; then
  version=$(cat "$VERSION_FILE" | tr -d '[:space:]')
  IFS='.' read -r major minor patch <<< "$version"
  patch=$((patch + 1))
  new_version="${major}.${minor}.${patch}"
  echo "$new_version" > "$VERSION_FILE"
  echo "========== Version bumped: ${version} -> ${new_version} =========="
else
  echo "1.0.0" > "$VERSION_FILE"
  new_version="1.0.0"
  echo "========== Version initialized: ${new_version} =========="
fi

ZIP_NAME="lamp-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-lamp/ota/lamp/${new_version}.zip}"

echo "========== Build lamp binary (VERSION=${new_version}) =========="
(cd "$ROOT_DIR" && make lamp-build VERSION="$new_version")

if [[ ! -f "$LAMP_BIN" ]]; then
  echo "Error: lamp binary not found at $LAMP_BIN after make lamp-build"
  exit 1
fi

echo "========== Zipping lamp binary to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$ROOT_DIR" && zip "$ZIP_PATH" "$LAMP_BIN")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (lamp/ota/metadata.json) - backend key
METADATA_PATH="lamp/ota/metadata.json"
METADATA_TMP=$(mktemp)
BACKEND_URL="${BACKEND_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

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
data.pop('lumi', None)
data['lamp'] = {'version': sys.argv[1], 'url': sys.argv[2]}
print(json.dumps(data, indent=2))
" "$new_version" "$BACKEND_URL")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (backend: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH" "$LAMP_BIN"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
