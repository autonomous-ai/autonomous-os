#!/bin/bash
# Run this on the Pi to download and execute the latest setup from CDN.
# Usage: curl -fsSL https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/install.sh | sudo bash
set -euo pipefail

# OTA metadata URL — the per-deployment source of truth for this install path.
# Override by exporting OTA_METADATA_URL before running. setup.sh consumes it.
OTA_METADATA_URL="${OTA_METADATA_URL:-https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/ota/metadata.json}"

curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" \
  -o /tmp/setup.sh \
  "https://cdn.autonomous.ai/lamp/setup.sh"
chmod +x /tmp/setup.sh
OTA_METADATA_URL="$OTA_METADATA_URL" bash /tmp/setup.sh
