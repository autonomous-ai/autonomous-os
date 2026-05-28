#!/usr/bin/env bash
set -e

# Publish a new openclaw version to OTA metadata. Pi watcher reads
# metadata.json's openclaw.version and (when added) runs `npm install
# -g openclaw@<version>` + restarts the service. This script ONLY
# updates the metadata field — it doesn't touch GCS otherwise.
#
# Usage:
#   ./scripts/upload-openclaw.sh <version_str>
#
# Example:
#   ./scripts/upload-openclaw.sh 1.2.3
#
# Other keys in metadata.json (skills, etc.) are preserved.

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <openclaw-version>" >&2
  echo "Example: $0 1.2.3" >&2
  exit 1
fi
VERSION="$1"

GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"
METADATA_GCS="gs://${GCS_BUCKET}/lamp/ota/metadata.json"

METADATA_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP"' EXIT

# Pull existing metadata; if missing, bootstrap with an empty object.
if ! gsutil cp "$METADATA_GCS" "$METADATA_TMP" 2>/dev/null; then
  echo "Note: $METADATA_GCS not found — bootstrapping with empty object."
  echo "{}" > "$METADATA_TMP"
fi

python3 - "$METADATA_TMP" "$VERSION" <<'PY'
import json
import sys

path, version = sys.argv[1], sys.argv[2]
d = json.load(open(path))
oc = d.get("openclaw") if isinstance(d.get("openclaw"), dict) else {}
oc["version"] = version
d["openclaw"] = oc
json.dump(d, open(path, "w"), indent=4)
PY

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: openclaw.version = ${VERSION}"
