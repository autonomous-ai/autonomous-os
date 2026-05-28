#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
DIST_DIR="${PROJECT_ROOT}/lamp/web/dist"
ZIP_NAME="setup-web.zip"
ZIP_PATH="${PROJECT_ROOT}/${ZIP_NAME}"
VERSION_FILE="${PROJECT_ROOT}/lamp/VERSION_WEB"

# Bucket for web bundle
GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"

echo "========== npm install =========="
(cd "$PROJECT_ROOT/lamp/web" && npm install)

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

GCS_PATH="${GCS_PATH:-lamp/ota/web/${new_version}.zip}"

echo "========== npm run build =========="
(cd "$PROJECT_ROOT/lamp/web" && npm run build)

if [[ ! -d "$DIST_DIR" ]]; then
  echo "Error: dist not found at $DIST_DIR"
  exit 1
fi

cp "$VERSION_FILE" "$DIST_DIR/VERSION"

echo "========== Zipping dist contents to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$DIST_DIR" && zip -r "$ZIP_PATH" .)

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json (lamp/ota/metadata.json) - web key
METADATA_PATH="lamp/ota/metadata.json"
METADATA_TMP=$(mktemp)
WEB_URL="${WEB_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

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
data['web'] = {'version': sys.argv[1], 'url': sys.argv[2]}
print(json.dumps(data, indent=2))
" "$new_version" "$WEB_URL")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (web: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
