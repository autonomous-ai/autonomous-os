#!/usr/bin/env bash
set -e

# Publish a new OpenCode CLI version to OTA metadata. Mirrors
# upload-openclaw.sh / upload-codex.sh — this script ONLY updates the metadata
# field, it doesn't touch GCS otherwise (the artifact comes from the official
# opencode.ai installer; the device fetches it directly, no url/sha256 here).
#
# VERSION FORMAT: the bare semver (e.g. 1.18.4) — what `opencode --version`
# prints and what the official installer takes via `--version`.
#
# Usage:
#   ./scripts/release/upload-opencode.sh <version_str>
#
# Example:
#   ./scripts/release/upload-opencode.sh 1.18.4
#
# Bumping `version` alone does NOT push the fleet: the bootstrap worker only
# auto-applies up to `min_version`. Release it with:
#   make promote-opencode
#
# Other keys in metadata.json (skills, openclaw, codex, …) are preserved.

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <opencode-version>" >&2
  echo "Example: $0 1.18.4   (bare semver — no leading 'v')" >&2
  exit 1
fi
VERSION="$1"

# The value published here is compared against `opencode --version` output, so a
# "v"-prefixed string would never match and would re-trigger the update forever.
if [[ "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (1.18.4), not a v-prefixed tag ($VERSION)." >&2
  exit 1
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"
METADATA_GCS="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json"

METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP" "$PAYLOAD_TMP"' EXIT

# Pull existing metadata; if missing, bootstrap with an empty object.
if ! gsutil cp "$METADATA_GCS" "$METADATA_TMP" 2>/dev/null; then
  echo "Note: $METADATA_GCS not found — bootstrapping with empty object."
  printf '{}' > "$PAYLOAD_TMP"
else
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
fi

python3 - "$PAYLOAD_TMP" "$VERSION" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json
import sys

path, version, updated_at = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
oc = d.get("opencode") if isinstance(d.get("opencode"), dict) else {}
oc["version"] = version
oc["updated_at"] = updated_at
d["opencode"] = oc
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: opencode.version = ${VERSION}"
echo "Fleet is NOT updated yet — run 'make promote-opencode' to raise min_version."
