#!/bin/bash
# Run this on the Pi to download and execute the latest setup from CDN.
# Usage: curl -fsSL https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/install.sh | sudo bash
set -euo pipefail
curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" \
  -o /tmp/setup.sh \
  "https://cdn.autonomous.ai/lamp/setup.sh"
chmod +x /tmp/setup.sh
bash /tmp/setup.sh
