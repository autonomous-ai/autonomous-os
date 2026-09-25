#!/usr/bin/env bash
set -e

# Publish a new Hermes CLI version to OTA metadata. Mirrors upload-openclaw.sh /
# upload-codex.sh — this script ONLY updates the metadata field, it doesn't touch
# GCS otherwise (Hermes is a git install from github.com/NousResearch/hermes-agent;
# the device checks the commit out directly, so there is no url/sha256 here).
#
# PINNED BY COMMIT. Hermes has no package registry, only git, and `hermes update`
# takes no target (it moves to upstream HEAD). So the metadata carries the exact
# upstream COMMIT that reports `version`, and `software-update hermes` checks it
# out through the upstream installer's `--commit <sha> --force-commit` — the same
# flags scripts/imager/build-orangepi.sh bakes the image with. Upstream tags
# releases by date (v2026.9.7 → 0.21.1, v2026.9.11 → 0.21.2, v2026.9.14 →
# 0.21.3); pass the tag and this script resolves it to the peeled commit.
#
# VERSION FORMAT: the bare semver as printed by `hermes --version`; it is what the
# updater and the bootstrap worker compare against, so it must match exactly.
#
# Usage:
#   ./scripts/release/upload-hermes.sh <version_str> <upstream-tag|commit-sha>
#   ./scripts/release/upload-hermes.sh 0.21.1 v2026.9.7
#
# Bumping `version` alone does NOT push the fleet: the bootstrap worker only
# auto-applies up to `min_version`. Release it with:
#   make promote-hermes
#
# Other keys in metadata.json (skills, openclaw, codex, …) are preserved.

if [[ -z "${1:-}" || -z "${2:-}" ]]; then
  echo "Usage: $0 <hermes-version> <upstream-tag|commit-sha>" >&2
  echo "Example: $0 0.21.1 v2026.9.7   (bare semver — no leading 'v'; tag as published upstream)" >&2
  exit 1
fi
VERSION="$1"
REF="$2"
HERMES_REPO="https://github.com/NousResearch/hermes-agent"

# The value published here is compared against `hermes --version` output, so a
# "v"-prefixed string would never match and would re-trigger the update forever.
if [[ "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (0.21.1), not a v-prefixed tag ($VERSION)." >&2
  exit 1
fi

# Resolve the ref to a full 40-char commit: the upstream installer rejects
# abbreviated SHAs, and a tag must be PEELED (^{}) — annotated tags otherwise
# resolve to the tag object, which the installer cannot check out.
if [[ "$REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  COMMIT="$REF"
else
  COMMIT="$(git ls-remote --tags "$HERMES_REPO" "refs/tags/${REF}^{}" | awk '{print $1}' | head -1)"
  [[ -n "$COMMIT" ]] || COMMIT="$(git ls-remote --tags "$HERMES_REPO" "refs/tags/${REF}" | awk '{print $1}' | head -1)"
  if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "ERROR: could not resolve '$REF' to a commit on $HERMES_REPO (try: git ls-remote --tags $HERMES_REPO)" >&2
    exit 1
  fi
fi
echo "hermes $VERSION → $REF = $COMMIT"

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

python3 - "$PAYLOAD_TMP" "$VERSION" "$COMMIT" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json
import sys

path, version, commit, updated_at = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
d = json.load(open(path))
hm = d.get("hermes") if isinstance(d.get("hermes"), dict) else {}
hm["version"] = version
hm["commit"] = commit
hm["updated_at"] = updated_at
d["hermes"] = hm
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: hermes.version = ${VERSION}, hermes.commit = ${COMMIT}"
echo "Fleet is NOT updated yet — run 'make promote-hermes' to raise min_version."
