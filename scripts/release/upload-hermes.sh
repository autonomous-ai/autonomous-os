#!/usr/bin/env bash
set -e

# Publish a new Hermes CLI version to OTA metadata. Mirrors upload-openclaw.sh /
# upload-codex.sh — this script ONLY updates the metadata field, it doesn't touch
# GCS otherwise (Hermes is a git install from hermes-agent.nousresearch.com; the
# device pulls it directly, so there is no url/sha256 here).
#
# ⚠️ NOT PINNABLE, unlike every other component. Hermes is installed from git
# and its updater (`hermes update`, enabled by the `.install_method=git` stamp
# written in runtimes/hermes/install.sh) always moves to upstream HEAD — it takes
# no target version. So the number published here is the version you EXPECT a
# device to land on: it drives WHEN the fleet updates (via min_version), not
# WHICH build it gets. `software-update hermes` warns, and does not fail, when
# the resulting `hermes --version` differs.
#
# VERSION FORMAT: the bare semver as printed by `hermes --version`.
#
# Usage:
#   ./scripts/release/upload-hermes.sh <version_str>
#
# Bumping `version` alone does NOT push the fleet: the bootstrap worker only
# auto-applies up to `min_version`. Release it with:
#   make promote-hermes
#
# Other keys in metadata.json (skills, openclaw, codex, …) are preserved.

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <hermes-version>" >&2
  echo "Example: $0 0.5.2   (bare semver — no leading 'v')" >&2
  exit 1
fi
VERSION="$1"

# The value published here is compared against `hermes --version` output, so a
# "v"-prefixed string would never match and would re-trigger the update forever.
if [[ "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (0.5.2), not a v-prefixed tag ($VERSION)." >&2
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
hm = d.get("hermes") if isinstance(d.get("hermes"), dict) else {}
hm["version"] = version
hm["updated_at"] = updated_at
d["hermes"] = hm
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: hermes.version = ${VERSION}"
echo "Fleet is NOT updated yet — run 'make promote-hermes' to raise min_version."
