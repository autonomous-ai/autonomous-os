#!/usr/bin/env bash
#
# Promote an OTA component's auto-rollout floor (min_version) so the bootstrap
# worker pushes the build to the whole fleet.
#
# Staged-rollout model:
#   - `upload-<component>.sh` bumps `version` (the build anyone CAN pull manually
#     via `software-update <key>` over SSH) but PRESERVES `min_version`, so the
#     auto worker does NOT push it.
#   - This script bumps `min_version` (default: up to the current `version`).
#     Bootstrap auto-updates every device whose current version is below it.
#
# Usage:
#   ./scripts/release/promote-ota.sh <component> [min_version]
#     <component>  flat key (os-server | bootstrap | web | hal |
#                  claude-desktop-buddy | openclaw) OR device:<type> (e.g. device:lamp)
#     [min_version] optional explicit floor; defaults to the entry's current version
#
# Examples:
#   ./scripts/release/promote-ota.sh hal             # min_version = hal.version
#   ./scripts/release/promote-ota.sh os-server 1.4.0 # pin floor explicitly
#   ./scripts/release/promote-ota.sh device:lamp     # min_version = devices.lamp.version
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

component="${1:-}"
override_min="${2:-}"
# Accept `device <type> [min]` (make-friendly, no colon) as well as `device:<type> [min]`.
if [[ "$component" == "device" ]]; then
  dtype="${2:-}"
  if [[ -z "$dtype" ]]; then
    echo "Usage: $0 device <type> [min_version]   (e.g. device lamp)" >&2
    exit 1
  fi
  component="device:${dtype}"
  override_min="${3:-}"
fi
if [[ -z "$component" ]]; then
  echo "Usage: $0 <component> [min_version]   (flat key | device <type> | device:<type>)" >&2
  exit 1
fi

METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_GCS="gs://${GCS_BUCKET}/${METADATA_PATH}"
METADATA_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP"' EXIT

echo "========== Fetch metadata from ${METADATA_GCS} =========="
gsutil cp "$METADATA_GCS" "$METADATA_TMP"

python3 - "$METADATA_TMP" "$component" "$override_min" <<'PY'
import json, sys

path, comp, override = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))

if comp.startswith("device:"):
    dtype = comp.split(":", 1)[1]
    entry = data.get("devices", {}).get(dtype)
    where = "devices.%s" % dtype
else:
    entry = data.get(comp)
    where = comp

if not isinstance(entry, dict):
    sys.exit("no metadata entry for %s" % where)

target = override or entry.get("version")
if not target:
    sys.exit("%s has no version to promote to" % where)

old = entry.get("min_version", "(unset -> version)")
entry["min_version"] = target
json.dump(data, open(path, "w"), indent=2)
print("%s: min_version %s -> %s" % (where, old, target))
PY

echo "========== Upload metadata to ${METADATA_GCS} =========="
gsutil -h "Content-Type:application/json" \
       -h "Cache-Control:no-cache, no-store, must-revalidate" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Promoted. Devices with current version below min_version will now auto-update."
