#!/usr/bin/env bash
# spike-agent.sh — install the OpenClaw gateway on a Reachy Mini.
#
# RUNS ON THE ROBOT. Takes the openclaw version from OTA metadata, installs that
# exact version from npm, seeds /root/.openclaw, and runs the gateway under
# systemd. Mirrors stage_openclaw in scripts/provision/setup.sh and the
# software-update openclaw branch in scripts/imager/build-orangepi.sh, minus the
# parts Reachy does not need.
#
# This used to run from a Mac over ssh with the version hardcoded in the script.
# Both were wrong: the pin drifted from what the fleet actually runs the moment
# anyone published a new build, and a spike that reproduces a bug against a
# version nobody else has says nothing about anyone else's robot.
#
# openclaw is the one OTA component with no url — it is an npm package, so the
# metadata carries only a version and ota_unpack/ota_install_binary do not apply
# here. `ota_field openclaw version` is still the single source of truth, the
# same field the bootstrap worker reconciles against (system/bootstrap).
#
# Why it matters beyond chat: os-server's status reporter skips every tick until
# the agent gateway is ready (system/device/status_reporter.go), so the backend
# never assigns a device_id and MQTT stays subscribed to an empty channel. No
# gateway means no cloud identity.
#
# Deltas from the lamp install, deliberate:
#   - no chromium / xvfb, and therefore no PUPPETEER_EXECUTABLE_PATH / CHROME_BIN
#     in the unit: they back the gateway's headless-browser tooling, which a
#     screenless desk robot has no use for, and they cost ~600 MB on a 14 GB
#     eMMC that ships ~60% full and is the same disk HAL's ~2 GB venv lands on.
#     Add them back if a browser-driving skill is ever wanted here.
#   - no skills seeding: os-server re-syncs and prunes skills on boot.
#
# Usage:
#   sudo bash spike-agent.sh              # install + start
#   sudo OPENCLAW_VERSION=2026.6.11 \
#        bash spike-agent.sh              # override the OTA pin to test a build
#   sudo bash spike-agent.sh --stop
#   sudo bash spike-agent.sh --uninstall  # stop + remove the unit and the package
set -euo pipefail

SPIKE_TAG="spike-agent"
# shellcheck source=spike-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/spike-lib.sh"

SERVICE="openclaw"
# The dot matters: os-server writes openclaw.json here and the gateway reads it
# from the same path. A /root/openclaw vs /root/.openclaw mismatch is not a
# missing-file error — the gateway boots with its own fresh token and every WS
# connect from os-server closes with 1008 / token_mismatch.
OPENCLAW_HOME="/root/.openclaw"

STOP_ONLY=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --stop)      STOP_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) die "unknown flag: $arg" ;;
  esac
done

ensure_root "$@"

if [ "$STOP_ONLY" = "1" ] || [ "$UNINSTALL" = "1" ]; then
  say "Stopping the OpenClaw gateway"
  stop_unit "$SERVICE"
  if [ "$UNINSTALL" = "1" ]; then
    remove_unit "$SERVICE"
    npm uninstall -g openclaw >/dev/null 2>&1 || true
    info "removed the unit and the global openclaw package"
    info "kept $OPENCLAW_HOME — it holds the gateway token os-server has cached,"
    info "plus agent sessions and credentials; deleting it forces a re-onboard"
  else
    info "stopped; still enabled, so it returns on the next boot (--uninstall to prevent that)"
  fi
  exit 0
fi

say "1/5  Resolve the version from OTA"
ensure_tools
# The npm spec is derived from the metadata version, but they are not always
# byte-identical: the feed publishes zero-padded months ("2026.06.10") while npm
# publishes "2026.6.10". npm parses the spec as a semver range, a leading zero
# makes it an invalid range, and npm then falls back to reading it as a dist-tag
# — the install dies with "No matching version found". Strip the padding.
OTA_VERSION="$(ota_field openclaw version)"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-$OTA_VERSION}"
[ -n "$OPENCLAW_VERSION" ] || die "OTA metadata has no openclaw version, and OPENCLAW_VERSION is unset"
NPM_SPEC="$(echo "$OPENCLAW_VERSION" | sed -E 's/(^|\.)0+([0-9])/\1\2/g')"
info "OTA pin: ${OTA_VERSION:-<none>}   installing: $NPM_SPEC"

say "2/5  Install Node.js 22"
# Checked before npm runs, not after: the openclaw package tree is ~400 MB
# unpacked and a half-written global install on a full disk leaves a binary on
# PATH that cannot start, which reads exactly like a code bug.
avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
info "free space: $(awk -v k="$avail_kb" 'BEGIN {printf "%.1f", k/1048576}') GB"
[ "$avail_kb" -ge 1572864 ] || die "need at least 1.5 GB free on / for node + the openclaw package"

# NodeSource, not apt: Debian trixie ships node 20, and openclaw hard-refuses to
# start on anything below 22.19 ("Node.js v22.19+ is required"). npm only WARNs
# at install time, so on the shipped OS the failure surfaces as a silent systemd
# crash-loop hours later instead of an install error. Same source and major as
# the lamp (scripts/provision/setup.sh, scripts/imager/build-orangepi.sh).
node_major() { node --version 2>/dev/null | sed 's/^v//; s/\..*//'; }
if [ "$(node_major)" ] && [ "$(node_major)" -ge 22 ] 2>/dev/null; then
  info "node $(node --version), npm $(npm --version) already present"
else
  info "installing Node.js 22 (NodeSource)"
  retry "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -" 3 \
    || die "NodeSource setup script failed"
  apt-get install -y nodejs || die "apt-get install nodejs failed"
  info "installed node $(node --version), npm $(npm --version)"
fi
# Fail here rather than let the gateway crash-loop: an apt pin or a leftover
# /usr/local/bin/node can still win on PATH after NodeSource succeeds.
[ "$(node_major)" -ge 22 ] 2>/dev/null || die "node is $(node --version 2>/dev/null || echo missing) — openclaw needs >= 22.19"

say "3/5  Install openclaw@$NPM_SPEC"
# The browser-download opt-outs matter at INSTALL time, not just at runtime:
# without them npm pulls a Chromium build for the capability this device does
# not declare, on the eMMC checked above.
installed="$(openclaw --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
if [ "$installed" = "$NPM_SPEC" ]; then
  info "openclaw $NPM_SPEC already installed"
else
  [ -n "$installed" ] && info "installed openclaw is $installed — replacing with $NPM_SPEC"
  # --omit=optional matches the imager: the optional deps are platform-specific
  # prebuilds that fall back to source builds on arm64 and cost minutes here.
  retry "env PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PUPPETEER_SKIP_DOWNLOAD=1 npm install -g --no-fund --no-audit --omit=optional 'openclaw@$NPM_SPEC'" 3 30 \
    || die "npm install openclaw@$NPM_SPEC failed"
fi
command -v openclaw >/dev/null || die "openclaw is not on PATH after install"
info "openclaw version: $(openclaw --version 2>&1 | head -1)"

say "4/5  Seed $OPENCLAW_HOME"
# Pre-creating the whole tree, not just the root: the gateway creates what it
# needs at runtime, but it does so with the umask it inherits, and 700 up front
# is what keeps credentials/ and the sessions transcripts off a shared robot.
mkdir -p \
  "$OPENCLAW_HOME" "$OPENCLAW_HOME/workspace" \
  "$OPENCLAW_HOME/agents/main/agent" "$OPENCLAW_HOME/agents/main/sessions" \
  "$OPENCLAW_HOME/credentials" "$OPENCLAW_HOME/.cache" "$OPENCLAW_HOME/.config" \
  "$OPENCLAW_HOME/.local/share" /var/log/openclaw
for p in "$OPENCLAW_HOME" "$OPENCLAW_HOME/workspace" "$OPENCLAW_HOME/agents" \
         "$OPENCLAW_HOME/agents/main" "$OPENCLAW_HOME/agents/main/agent" \
         "$OPENCLAW_HOME/agents/main/sessions" "$OPENCLAW_HOME/credentials" \
         "$OPENCLAW_HOME/.cache" "$OPENCLAW_HOME/.config" "$OPENCLAW_HOME/.local" \
         "$OPENCLAW_HOME/.local/share"; do
  chmod 700 "$p" 2>/dev/null || true
done

# Never regenerate the token on an existing install: os-server caches it, and a
# mismatch closes the gateway WS with 1008 / token_mismatch. The file also holds
# onboarding state, model defaults and MCP connector entries that os-server
# wrote (system/server/config_watch.go) — a re-seed silently drops all of it.
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
  info "openclaw.json already present — keeping it (token preserved)"
else
  TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat >"$OPENCLAW_HOME/openclaw.json" <<JSON
{
  "agents": {
    "defaults": {
      "workspace": "$OPENCLAW_HOME/workspace"
    }
  },
  "gateway": {
    "mode": "local",
    "bind": "loopback",
    "port": 18789,
    "auth": {
      "mode": "token",
      "token": "$TOKEN"
    },
    "controlUi": {
      "allowedOrigins": ["http://127.0.0.1", "http://localhost"],
      "allowInsecureAuth": false
    }
  }
}
JSON
  chmod 600 "$OPENCLAW_HOME/openclaw.json"
  info "seeded openclaw.json with a fresh gateway token"
fi

say "5/5  Install the unit and start"
# The gateway was the first spike to need a unit rather than tmux: os-server
# restarts it with `systemctl restart openclaw.service` after onboarding writes
# config, so a tmux-run gateway would be un-restartable by the very service that
# drives it. spike-hal.sh and spike-os.sh install units of their own now too.
OPENCLAW_BIN="$(command -v openclaw)"
write_unit "$SERVICE" <<UNIT
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$OPENCLAW_HOME
Environment="OPENCLAW_HOME=$OPENCLAW_HOME"
Environment="OPENCLAW_STATE_DIR=$OPENCLAW_HOME"
# HOME stays /root while the XDG dirs point into $OPENCLAW_HOME: the gateway
# scatters caches and state across all three, and letting them land in /root
# means --uninstall leaves them behind and a token can outlive its config.
Environment="HOME=/root"
Environment="XDG_CACHE_HOME=$OPENCLAW_HOME/.cache"
Environment="XDG_CONFIG_HOME=$OPENCLAW_HOME/.config"
Environment="XDG_DATA_HOME=$OPENCLAW_HOME/.local/share"
Environment="PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"
LimitNOFILE=65535
MemoryMax=1500M
ExecStart=$OPENCLAW_BIN gateway run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openclaw

[Install]
WantedBy=multi-user.target
UNIT

start_unit "$SERVICE"
# Longer than the 2s the other spike scripts wait: the gateway loads its plugin
# tree before it binds, and a node-version refusal only shows up after the first
# RestartSec=5, so a shorter check would call a crash-loop healthy.
sleep 8
if systemctl is-active --quiet "$SERVICE"; then
  info "running"
else
  info "WARN: not running — journalctl -u $SERVICE -n 50"
  info "WARN: 'Node.js v22.19+ is required' there means something outranks NodeSource on PATH"
fi
# The port, not just the unit state: the gateway can stay 'active' with its
# plugin load wedged, and os-server dials the socket, not systemd.
if command -v ss >/dev/null && ! ss -ltn 2>/dev/null | grep -q ':18789'; then
  info "WARN: nothing is listening on 18789 yet — os-server's WS dial will fail until it is"
fi

cat <<EOF

========================================
  OpenClaw gateway running under systemd
    version : $(openclaw --version 2>&1 | head -1)
    OTA pin : ${OTA_VERSION:-<none>}
    logs    : journalctl -u $SERVICE -f
    port    : 18789 (loopback) — os-server dials ws://127.0.0.1:18789
    state   : $OPENCLAW_HOME
    stop    : sudo bash spike-agent.sh --stop
========================================

Once the gateway reports ready, os-server's status reporter starts pinging and
the backend assigns device_id + the MQTT fa/fd channels. If os-server is not
installed yet, run spike-os.sh.
EOF
