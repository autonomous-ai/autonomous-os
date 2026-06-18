#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

DIST_DIR="${ROOT_DIR}/os/services/web/dist"
ZIP_NAME="setup-web.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
VERSION_FILE="${ROOT_DIR}/os/services/VERSION_WEB"

# Bucket for web bundle

echo "========== npm install =========="
(cd "$ROOT_DIR/os/services/web" && npm install)

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

GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/web/${new_version}.zip}"

echo "========== npm run build =========="
(cd "$ROOT_DIR/os/services/web" && npm run build)

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

# Update metadata.json (${BUCKET_PREFIX}/ota/metadata.json) - web key
METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
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
entry = data.get('web') if isinstance(data.get('web'), dict) else {}
entry.update({'version': sys.argv[1], 'url': sys.argv[2], 'updated_at': sys.argv[3]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['web'] = entry
print(json.dumps(data, indent=2))
" "$new_version" "$WEB_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (web: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
