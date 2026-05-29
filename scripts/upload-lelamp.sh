#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LELAMP_DIR="${ROOT_DIR}/lelamp"
VERSION_FILE="${ROOT_DIR}/lelamp/${VERSION_FILE:-VERSION_LELAMP}"

# Bucket and path: lamp/ota/lelamp/[semver].zip
GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"

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

ZIP_NAME="lelamp-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-lamp/ota/lelamp/${new_version}.zip}"

if [[ ! -d "$LELAMP_DIR" ]]; then
  echo "Error: lelamp directory not found at $LELAMP_DIR"
  exit 1
fi

echo "========== Zipping lelamp to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$LELAMP_DIR" && zip -r "$ZIP_PATH" . \
  -x ".venv/*" "__pycache__/*" "*/__pycache__/*" ".git/*" "*.pyc" \
  "uv.lock" ".env" ".python-version" "test/*")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (lamp/ota/metadata.json) - lelamp key
METADATA_PATH="lamp/ota/metadata.json"
METADATA_TMP=$(mktemp)
LELAMP_URL="${LELAMP_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

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
data['lelamp'] = {'version': sys.argv[1], 'url': sys.argv[2], 'updated_at': sys.argv[3]}
print(json.dumps(data, indent=2))
" "$new_version" "$LELAMP_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (lelamp: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
