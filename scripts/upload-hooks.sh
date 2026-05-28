#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOOKS_DIR="${ROOT_DIR}/lamp/resources/openclaw-hooks"

GCS_BUCKET="${GCS_BUCKET:-s3-autonomous-upgrade-3}"
GCS_PREFIX="${GCS_PREFIX:-lamp/hooks}"

if [[ ! -d "$HOOKS_DIR" ]]; then
  echo "Error: hooks directory not found at $HOOKS_DIR"
  exit 1
fi

count=0
for hook_dir in "$HOOKS_DIR"/*/; do
  [[ -d "$hook_dir" ]] || continue
  hook_name="$(basename "$hook_dir")"
  for f in "$hook_dir"HOOK.md "$hook_dir"handler.ts; do
    [[ -f "$f" ]] || continue
    filename="$(basename "$f")"
    gcs_path="${GCS_PREFIX}/${hook_name}/${filename}"
    echo "========== Upload ${hook_name}/${filename} to gs://${GCS_BUCKET}/${gcs_path} =========="
    gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$f" "gs://${GCS_BUCKET}/${gcs_path}"
    count=$((count + 1))
  done
done

echo "Done: uploaded ${count} file(s) to gs://${GCS_BUCKET}/${GCS_PREFIX}/"
