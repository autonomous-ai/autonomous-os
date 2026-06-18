#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

HAL_DIR="${ROOT_DIR}/os/hal"
VERSION_FILE="${ROOT_DIR}/os/hal/${VERSION_FILE:-VERSION_HAL}"

# Bucket and path: ${BUCKET_PREFIX}/ota/hal/[semver].zip

# Auto-increment semver (patch) before upload
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

ZIP_NAME="hal-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/hal/${new_version}.zip}"

if [[ ! -d "$HAL_DIR" ]]; then
  echo "Error: hal directory not found at $HAL_DIR"
  exit 1
fi

echo "========== Zipping hal to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$HAL_DIR" && zip -r "$ZIP_PATH" . \
  -x ".venv/*" "__pycache__/*" "*/__pycache__/*" ".git/*" "*.pyc" \
  "uv.lock" ".env" ".python-version" "test/*")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (${BUCKET_PREFIX}/ota/metadata.json) - hal key
METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
HAL_URL="${HAL_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

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
entry = data.get('hal') if isinstance(data.get('hal'), dict) else {}
entry.update({'version': sys.argv[1], 'url': sys.argv[2], 'updated_at': sys.argv[3]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['hal'] = entry
print(json.dumps(data, indent=2))
" "$new_version" "$HAL_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (hal: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
