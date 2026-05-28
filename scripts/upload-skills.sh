#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILLS_DIR="${ROOT_DIR}/lamp/resources/openclaw-skills"

GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"
GCS_PREFIX="${GCS_PREFIX:-lamp/skills}"

if [[ ! -d "$SKILLS_DIR" ]]; then
  echo "Error: skills directory not found at $SKILLS_DIR"
  exit 1
fi

# Local hash cache to skip unchanged skills (whole folder).
HASH_CACHE="${SCRIPT_DIR}/.skill-hashes"
touch "$HASH_CACHE"

WORK_DIR="$(mktemp -d)"
ENTRIES_FILE="$(mktemp)"
trap 'rm -rf "$WORK_DIR" "$ENTRIES_FILE"' EXIT

count=0
skipped=0

for skill_dir in "$SKILLS_DIR"/*/; do
  [[ -d "$skill_dir" ]] || continue
  skill_name="$(basename "$skill_dir")"

  zip_path="${WORK_DIR}/${skill_name}.zip"
  # Build deterministic zip + hash via embedded Python so the same
  # content always produces the same bytes (regardless of upload host's
  # mtimes / locale). No external "zip" tool dependency.
  skill_hash="$(python3 - "$skill_dir" "$zip_path" <<'PY'
import hashlib
import os
import sys
import zipfile
from pathlib import Path

skill_dir = Path(sys.argv[1]).resolve()
out_path = Path(sys.argv[2])

EXCLUDE_NAMES = {
    ".DS_Store", "Thumbs.db",
    ".git", ".gitignore", ".gitattributes",
    "__pycache__", ".pytest_cache",
    ".idea", ".vscode",
    "node_modules",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp", ".log")


def is_excluded(p: Path) -> bool:
    name = p.name
    if name in EXCLUDE_NAMES:
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    return False


def has_excluded_ancestor(file_path: Path, root: Path) -> bool:
    cur = file_path.parent
    while True:
        if cur == root or cur == cur.parent:
            return False
        if is_excluded(cur):
            return True
        cur = cur.parent


# Sorted recursive walk → stable ordering of zip entries.
files = []
for p in sorted(skill_dir.rglob("*")):
    if not p.is_file():
        continue
    if is_excluded(p) or has_excluded_ancestor(p, skill_dir):
        continue
    files.append(p)

# Deterministic zip:
# - fixed timestamp (1980-01-01 — earliest zip-allowed)
# - fixed unix mode 0644
# - sorted entries
# Same content → same zip bytes → same sha256.
with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for f in files:
        arcname = str(f.relative_to(skill_dir))
        info = zipfile.ZipInfo(arcname)
        info.date_time = (1980, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(f, "rb") as src:
            zf.writestr(info, src.read())

h = hashlib.sha256()
with open(out_path, "rb") as fh:
    for chunk in iter(lambda: fh.read(65536), b""):
        h.update(chunk)
print(h.hexdigest()[:12])
PY
)"

  echo "${skill_name}|${skill_hash}" >> "$ENTRIES_FILE"

  # Skip if combined hash unchanged since last upload.
  cached_hash="$(grep "^${skill_name}:" "$HASH_CACHE" 2>/dev/null | cut -d: -f2 || true)"
  if [[ "$cached_hash" == "$skill_hash" ]]; then
    skipped=$((skipped + 1))
    continue
  fi

  gcs_path="${GCS_PREFIX}/${skill_name}.zip"
  echo "========== Upload ${skill_name}.zip (${skill_hash}) to gs://${GCS_BUCKET}/${gcs_path} =========="
  gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
         -h "Content-Type:application/zip" \
         cp "$zip_path" "gs://${GCS_BUCKET}/${gcs_path}"

  grep -v "^${skill_name}:" "$HASH_CACHE" > "${HASH_CACHE}.tmp" 2>/dev/null || true
  echo "${skill_name}:${skill_hash}" >> "${HASH_CACHE}.tmp"
  mv "${HASH_CACHE}.tmp" "$HASH_CACHE"
  count=$((count + 1))
done

echo "Done: uploaded ${count} skill zip(s), skipped ${skipped} unchanged. gs://${GCS_BUCKET}/${GCS_PREFIX}/"

# Update OTA metadata with per-skill { version }. Pi watcher reads this
# and refetches any skill whose version changed.
METADATA_GCS="gs://${GCS_BUCKET}/lamp/ota/metadata.json"
METADATA_TMP=$(mktemp)
trap 'rm -rf "$WORK_DIR" "$ENTRIES_FILE" "$METADATA_TMP"' EXIT
if gsutil cp "$METADATA_GCS" "$METADATA_TMP" 2>/dev/null; then
  python3 - "$METADATA_TMP" "$ENTRIES_FILE" <<'PY'
import json
import sys

metadata_path, entries_path = sys.argv[1], sys.argv[2]
d = json.load(open(metadata_path))
skills = {}
for line in open(entries_path):
    line = line.strip()
    if not line or "|" not in line:
        continue
    name, version = line.split("|", 1)
    skills[name] = {"version": version}
d["skills"] = skills
json.dump(d, open(metadata_path, "w"), indent=4)
PY
  gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
         -h "Content-Type:application/json" \
         cp "$METADATA_TMP" "$METADATA_GCS"
  echo "Updated metadata.json with per-skill versions"
else
  echo "Warning: could not update metadata.json"
fi
