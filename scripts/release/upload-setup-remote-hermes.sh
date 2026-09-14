#!/usr/bin/env bash
# Publish scripts/tools/setup-remote-hermes.sh to the CDN at
#   {CDN}/tools/setup-remote-hermes.sh
# so the "How do I get the URL + API Key?" popup on the device Runtime page
# resolves to a fresh copy. Unversioned static tool — every publish
# overwrites the same object. Cache-control is short (5 min) so the next
# revision reaches operators without a filename bump.
#
# Requires the intern-gcs service account to be active in gcloud (same one
# used by every other upload-*.sh here).
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

SCRIPT="${ROOT_DIR}/scripts/tools/setup-remote-hermes.sh"
DEST="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh"

[ -f "$SCRIPT" ] || { echo "Error: $SCRIPT not found" >&2; exit 1; }
bash -n "$SCRIPT" || { echo "Error: $SCRIPT failed syntax check" >&2; exit 1; }

echo "==> uploading $SCRIPT"
echo "    → $DEST"
gcloud storage cp --cache-control="public, max-age=300" \
  "$SCRIPT" "$DEST"

echo "==> public URL"
echo "    https://cdn.autonomous.ai/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh"
echo
echo "Cloudflare edges may still hold an older copy; force a fresh fetch with:"
echo "  curl -fsSL \"https://cdn.autonomous.ai/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh?v=\$(uuidgen)\""
