#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

BUDDY_DIR="${ROOT_DIR}/companions/claude-desktop-buddy"
VERSION_FILE="${BUDDY_DIR}/VERSION_BUDDY"

# Bucket and path: ${BUCKET_PREFIX}/ota/claude-desktop-buddy/[semver].zip

# Build for linux/arm64
echo "========== Building buddy-plugin (linux/arm64) =========="
(cd "$BUDDY_DIR" && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w" -o buddy-plugin .)

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

ZIP_NAME="claude-desktop-buddy-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/claude-desktop-buddy/${new_version}.zip}"

echo "========== Zipping to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$BUDDY_DIR" && zip -r "$ZIP_PATH" \
  buddy-plugin \
  config/buddy.json)

# Clean up binary (covers both `-o buddy-plugin` artifact and default
# `go build` output which uses the module name `claude-desktop-buddy`).
rm -f "${BUDDY_DIR}/buddy-plugin" "${BUDDY_DIR}/claude-desktop-buddy"

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

# Update metadata.json - claude-desktop-buddy key
METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
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
entry = data.get('claude-desktop-buddy') if isinstance(data.get('claude-desktop-buddy'), dict) else {}
entry.update({'version': sys.argv[1], 'url': sys.argv[2], 'updated_at': sys.argv[3]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['claude-desktop-buddy'] = entry
print(json.dumps(data, indent=2))
" "$new_version" "$BUDDY_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')")

echo "$updated_metadata" > "$METADATA_TMP"
echo "========== Upload metadata (claude-desktop-buddy: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
