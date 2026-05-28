#!/usr/bin/env bash
# Creates an annotated git tag whose message embeds the current OTA metadata
# snapshot from CDN, then pushes to origin. Lets buyers map the version
# string on the board ("lamp-server --version") back to a specific commit +
# component version set in the public repo (GPL v3 §6 compliance).
#
# Usage:
#   scripts/tag-release.sh v0.0.8
set -euo pipefail

VERSION="${1:-}"
OTA_METADATA_URL="${OTA_METADATA_URL:-https://cdn.autonomous.ai/lamp/ota/metadata.json}"
REMOTE="${TAG_REMOTE:-origin}"

if [[ -z "$VERSION" ]]; then
  echo "Usage: scripts/tag-release.sh v0.0.X" >&2
  exit 1
fi

if git rev-parse "$VERSION" >/dev/null 2>&1; then
  echo "ERROR: tag $VERSION already exists locally. Delete first:" >&2
  echo "  git tag -d $VERSION" >&2
  exit 1
fi

if git ls-remote --tags --exit-code "$REMOTE" "refs/tags/$VERSION" >/dev/null 2>&1; then
  echo "ERROR: tag $VERSION already exists on $REMOTE. Pick a new version." >&2
  exit 1
fi

echo "==> Fetching OTA metadata: $OTA_METADATA_URL"
METADATA=$(curl -fsS "$OTA_METADATA_URL")
echo "$METADATA" | jq . >/dev/null

echo "==> Creating annotated tag $VERSION"
printf 'Release %s\n\nOTA metadata snapshot (%s):\n\n%s\n' \
  "$VERSION" "$OTA_METADATA_URL" "$METADATA" \
  | git tag -a "$VERSION" -F -

echo "==> Pushing to $REMOTE"
git push "$REMOTE" "$VERSION"

echo ""
echo "==> Done. Tag $VERSION pushed."
echo "    View: $(git config --get remote.${REMOTE}.url | sed -E 's#(git@|https://)([^:/]+)[:/]#https://\2/#; s/\.git$//')/releases/tag/$VERSION"
