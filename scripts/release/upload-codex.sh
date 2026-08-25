#!/usr/bin/env bash
set -e

# Publish a new Codex CLI version to OTA metadata. Mirrors
# upload-openclaw.sh — this script ONLY updates the metadata field, it
# doesn't touch GCS otherwise (the artifact lives in openai/codex GitHub
# releases; the device fetches it directly, so there is no url/sha256 here).
#
# VERSION FORMAT: the bare semver, WITHOUT the upstream "rust-v" tag prefix
# (e.g. 0.149.1, not rust-v0.149.1) — it is compared against `codex --version`
# output ("codex-cli 0.149.1") by the bootstrap worker, and the on-device
# updater re-adds the prefix when building the release URL.
#
# Usage:
#   ./scripts/release/upload-codex.sh <version_str>
#
# Example:
#   ./scripts/release/upload-codex.sh 0.149.1
#
# Bumping `version` alone does NOT push the fleet: the bootstrap worker only
# auto-applies up to `min_version`. Release it with:
#   make promote-codex          # min_version = codex.version
#
# Other keys in metadata.json (skills, openclaw, etc.) are preserved.

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <codex-version>" >&2
  echo "Example: $0 0.149.1   (bare semver — no 'rust-v' prefix)" >&2
  exit 1
fi
VERSION="$1"

# Reject the upstream tag prefix explicitly rather than silently stripping it:
# the value published here is compared against `codex --version` output, and a
# "rust-v0.149.1" in metadata would never match and would re-trigger forever.
if [[ "$VERSION" == rust-v* || "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (0.149.1), not the release tag ($VERSION)." >&2
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
cx = d.get("codex") if isinstance(d.get("codex"), dict) else {}
cx["version"] = version
cx["updated_at"] = updated_at
d["codex"] = cx
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: codex.version = ${VERSION}"
echo "Fleet is NOT updated yet — run 'make promote-codex' to raise min_version."
