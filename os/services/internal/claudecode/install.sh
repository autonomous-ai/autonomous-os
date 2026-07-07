#!/usr/bin/env bash
# runtimes/claudecode/install.sh — installer for the Claude Code agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/claudecode/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to claudecode. It
# is self-contained: nothing in the imager or os-server knows about claudecode.
#
# This installer is self-sufficient: a direct `bash install.sh` fully configures
# AND starts the backend — it does not rely on switch-runtime to run the presync
# hook or enable the unit afterwards.
#
# What it does:
#   1. prerequisites: jq (presync config reads) + curl (CLI/bun downloads);
#   2. install the Claude Code CLI (native installer, linux arm64/amd64) and
#      symlink it to /usr/local/bin/claude;
#   3. install bun + the telegram channel plugin (best-effort — Claude Code
#      channel plugins are bun scripts; only needed when a telegram bot token is
#      configured, see https://code.claude.com/docs/en/channels);
#   4. run the presync hook (materialized by os-server BEFORE this installer):
#      it OWNS the launch env (/root/.claudecode/.env — ANTHROPIC_* from
#      config.json llm_*) and the telegram channel config
#      (~/.claude/channels/telegram/.env + access.json). See presync.sh.
#   5. write + start the systemd unit. Claude Code only runs in the foreground,
#      so the unit runs the Go gatewayd (`os-server claudecode-gatewayd` —
#      compiled into the os-server binary, nothing to materialize), which holds
#      the headless Claude process and exposes the WebSocket os-server
#      connects to.
#
# UNIT NAME: claudecode.service (== runtime name). No service-name declaration
#    file is needed (switch_runtime.sh defaults the unit to the runtime name).
#    The unit body MUST stay in sync with internal/claudecode/gateway_unit.go
#    claudecodeUnitContent (EnsureOnboarding's self-heal writer).
#
# ⚠️ VERIFY ON DEVICE: the gatewayd must listen on 127.0.0.1:18791 and serve the
#    WebSocket at /claude/ws/ to match internal/claudecode/constants.go WSURL,
#    and its bearer token must equal constants.go Token (both compiled into the
#    os-server binary).
set -euo pipefail

# Tee all output to a log under /root/.claudecode (persistent rootfs), NOT
# /var/log — on these boards /var/log is a volatile zram mount wiped on reboot.
CC_LOG="${CC_LOG:-/root/.claudecode/install.log}"
mkdir -p "$(dirname "$CC_LOG")"
exec > >(tee -a "$CC_LOG") 2>&1
echo "[install-claudecode] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $CC_LOG) ====="

export HOME=/root
CLAUDE_BIN="/usr/local/bin/claude"

echo "[install-claudecode] prerequisites (jq, curl)"
apt-get update || true
apt-get install -y jq curl || true

echo "[install-claudecode] install Claude Code CLI"
if ! command -v claude >/dev/null 2>&1 && [ ! -x /root/.local/bin/claude ]; then
  # Official native installer — standalone binary, supports linux arm64/amd64,
  # installs to ~/.local/bin/claude. No Node.js required.
  curl -fsSL https://claude.ai/install.sh | bash
fi
if [ -x /root/.local/bin/claude ]; then
  ln -sf /root/.local/bin/claude "$CLAUDE_BIN"
fi
command -v claude >/dev/null 2>&1 || {
  echo "[install-claudecode] ERROR: claude CLI not found after install" >&2
  exit 1
}
claude --version || true

echo "[install-claudecode] install bun (channel plugins are bun scripts)"
if ! command -v bun >/dev/null 2>&1 && [ ! -x /root/.bun/bin/bun ]; then
  curl -fsSL https://bun.sh/install | bash || echo "[install-claudecode] WARN: bun install failed — telegram channel plugin will not run"
fi
if [ -x /root/.bun/bin/bun ]; then
  ln -sf /root/.bun/bin/bun /usr/local/bin/bun
fi

# Channel plugins (best-effort). Channels are a Claude Code research preview:
# if the plugin CLI or marketplace is unavailable on this build, the device
# still works — voice/web/sensing flow through the bridge; only the channel
# receive loops are skipped (presync leaves CLAUDECODE_CHANNELS empty when no
# token is configured anyway). telegram + discord are the two channels the
# claudecode runtime declares (SupportedChannels); slack has no Claude Code
# channel plugin ("Claude in Slack" is a separate cloud feature).
echo "[install-claudecode] add plugin marketplace + telegram/discord channel plugins (best-effort)"
"$CLAUDE_BIN" plugin marketplace add anthropics/claude-plugins-official \
  || echo "[install-claudecode] WARN: marketplace add failed (offline or older CLI?)"
for ch in telegram discord; do
  "$CLAUDE_BIN" plugin install "${ch}@claude-plugins-official" \
    || echo "[install-claudecode] WARN: $ch plugin install failed — $ch channel unavailable until installed"
done

# Env + channel config are owned ENTIRELY by the presync hook, NOT written
# here (the bridge itself ships inside the os-server binary). The hook is
# materialized to /usr/local/bin/runtime-claudecode-presync by os-server BEFORE
# this installer runs; switch-runtime re-runs it before every later start, and
# EnsureOnboarding re-runs it on every os-server boot — so the config
# self-heals (e.g. after a factory reset).
PRESYNC_HOOK="/usr/local/bin/runtime-claudecode-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-claudecode] sync env/channels now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-claudecode] WARN: presync failed (config.json missing? non-fatal — retried on next switch/boot)"
else
  echo "[install-claudecode] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); env NOT configured"
fi

# systemd unit — the Go gatewayd (compiled into os-server) wraps the
# foreground-only claude process so switch-runtime can enable/disable/verify it
# like any other backend. Unit name == runtime name (claudecode.service), so no
# service-name declaration file is needed. KEEP IN SYNC with
# internal/claudecode/gateway_unit.go.
echo "[install-claudecode] write systemd unit claudecode.service"
cat >/etc/systemd/system/claudecode.service <<UNIT
[Unit]
Description=Claude Code agent bridge (os-server claudecode-gatewayd holding one headless claude)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.claudecode/.env
WorkingDirectory=/root/.claudecode
ExecStart=/usr/local/bin/os-server claudecode-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-claudecode] enable + start claudecode.service"
systemctl enable --now claudecode.service
systemctl status claudecode.service --no-pager || true

# Verify hook: cheap + offline (CLI presence + os-server binary — the gatewayd
# ships inside os-server; per the adding-agent-runtime golden rule, presync
# heals everything else, so a structure check here would only force needless
# full reinstalls).
echo "[install-claudecode] declare verify hook for switch-runtime (claude + os-server)"
mkdir -p /usr/local/lib/os-runtimes/claudecode
cat >/usr/local/lib/os-runtimes/claudecode/verify <<'VERIFY'
#!/usr/bin/env bash
command -v claude >/dev/null 2>&1 && [ -x /usr/local/bin/os-server ]
VERIFY
chmod +x /usr/local/lib/os-runtimes/claudecode/verify

echo "[install-claudecode] done — claudecode gatewayd installed + started (claudecode.service)."
echo "[install-claudecode] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
