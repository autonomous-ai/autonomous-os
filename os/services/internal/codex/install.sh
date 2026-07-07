#!/usr/bin/env bash
# runtimes/codex/install.sh — installer for the Codex agentic backend.
#
# Published to the CDN at ${RUNTIMES_BASE_URL}/codex/install.sh and fetched by
# /usr/local/bin/switch-runtime the first time a device switches to codex. It
# is self-contained: nothing in the imager or os-server knows about codex.
#
# This installer is self-sufficient: a direct `bash install.sh` fully configures
# AND starts the backend — it does not rely on switch-runtime to run the presync
# hook or enable the unit afterwards.
#
# What it does:
#   1. prerequisites: jq (presync config reads) + curl (binary download);
#   2. install the OpenAI Codex CLI from a pinned GitHub release (static musl
#      binary per arch) to /usr/local/bin/codex;
#   3. run the presync hook (materialized by os-server BEFORE this installer):
#      it OWNS the Codex config (~/.codex/config.toml — model provider wiring
#      from config.json llm_*), the launch env (/root/.codex/.env), and the
#      one-time openclaw persona migration. See presync.sh.
#   4. write + start the systemd unit. Codex runs per-turn via `codex exec`, so
#      the unit runs the Go gatewayd (`os-server codex-gatewayd` — compiled into
#      the os-server binary, nothing to materialize), which drives the codex
#      process and exposes the WebSocket os-server connects to.
#
# UNIT NAME: codex.service (== runtime name). No service-name declaration
#    file is needed (switch_runtime.sh defaults the unit to the runtime name).
#
# ⚠️ VERIFY ON DEVICE: the gatewayd must listen on 127.0.0.1:18792 to match
#    internal/codex/constants.go, and its bearer token must equal constants.go
#    Token (both compiled into the os-server binary).
set -euo pipefail

# Tee all output to a log under /root/.codex (persistent rootfs), NOT
# /var/log — on these boards /var/log is a volatile zram mount wiped on reboot.
CODEX_LOG="${CODEX_LOG:-/root/.codex/install.log}"
mkdir -p "$(dirname "$CODEX_LOG")"
exec > >(tee -a "$CODEX_LOG") 2>&1
echo "[install-codex] ===== install start $(date -u '+%Y-%m-%dT%H:%M:%SZ') (log: $CODEX_LOG) ====="

export HOME=/root
CODEX_BIN="/usr/local/bin/codex"

# Pin the release the device installs. Bump here (and re-OTA os-server) to upgrade.
# Assets are published per-arch as codex-<triple>-unknown-linux-musl.tar.gz.
CODEX_VERSION="${CODEX_VERSION:-rust-v0.142.5}"
CODEX_REPO="${CODEX_REPO:-openai/codex}"

echo "[install-codex] prerequisites (jq, curl)"
apt-get update || true
apt-get install -y jq curl || true

echo "[install-codex] install Codex CLI ${CODEX_VERSION}"
case "$(uname -m)" in
  aarch64|arm64) CODEX_ASSET="codex-aarch64-unknown-linux-musl.tar.gz" ;;
  x86_64)        CODEX_ASSET="codex-x86_64-unknown-linux-musl.tar.gz" ;;
  *) echo "[install-codex] ERROR: unsupported arch $(uname -m) for codex"; exit 1 ;;
esac
# Idempotency: skip the download when the pinned version is already installed.
# `codex --version` prints like "codex-cli 0.142.5" — compare against the tag
# minus its "rust-v" prefix (tolerant substring match).
WANT_VERSION="${CODEX_VERSION#rust-v}"
if command -v codex >/dev/null 2>&1 && codex --version 2>/dev/null | grep -qF "$WANT_VERSION"; then
  echo "[install-codex] codex ${WANT_VERSION} already installed — skipping download"
else
  CODEX_URL="https://github.com/${CODEX_REPO}/releases/download/${CODEX_VERSION}/${CODEX_ASSET}"
  CODEX_TMP="$(mktemp -d)"
  echo "[install-codex] downloading ${CODEX_URL}"
  curl -fsSL "$CODEX_URL" -o "$CODEX_TMP/$CODEX_ASSET"
  tar -xzf "$CODEX_TMP/$CODEX_ASSET" -C "$CODEX_TMP"
  # The binary inside the tarball is named like the asset minus .tar.gz.
  install -m 0755 "$CODEX_TMP/${CODEX_ASSET%.tar.gz}" "$CODEX_BIN"
  rm -rf "$CODEX_TMP"
fi
"$CODEX_BIN" --version || {
  echo "[install-codex] ERROR: codex binary not runnable after install" >&2
  exit 1
}

mkdir -p /root/.codex/workspace /root/.codex/attachments

# config.toml + env + persona migration are owned ENTIRELY by the presync
# hook, NOT written here. The hook is materialized to
# /usr/local/bin/runtime-codex-presync by os-server BEFORE this installer runs;
# switch-runtime re-runs it before every later start, and EnsureOnboarding
# re-runs it on every os-server boot — so the config self-heals (e.g. after a
# factory reset).
PRESYNC_HOOK="/usr/local/bin/runtime-codex-presync"
if [ -x "$PRESYNC_HOOK" ]; then
  echo "[install-codex] sync config/env now (via $PRESYNC_HOOK)"
  "$PRESYNC_HOOK" \
    || echo "[install-codex] WARN: presync failed (config.json missing? non-fatal — retried on next switch/boot)"
else
  echo "[install-codex] WARN: $PRESYNC_HOOK absent — os-server did not materialize it (standalone/offline run?); config/env NOT synced"
fi

# systemd unit — the gatewayd drives `codex exec` per turn so switch-runtime
# can enable/disable/verify it like any other backend. Unit name == runtime
# name (codex.service), so no service-name declaration file is needed.
# MUST stay in sync with internal/codex/gateway_unit.go codexUnitContent
# (EnsureOnboarding self-heals the same unit on a hand-switched device).
echo "[install-codex] write systemd unit codex.service"
cat >/etc/systemd/system/codex.service <<UNIT
[Unit]
Description=Codex agent gateway (os-server codex-gatewayd driving \`codex exec\` per turn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.codex/.env
WorkingDirectory=/root/.codex
ExecStart=/usr/local/bin/os-server codex-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
echo "[install-codex] enable + start codex.service"
systemctl enable --now codex.service
systemctl status codex.service --no-pager || true

# Verify hook: cheap + offline (CLI presence + os-server binary — the gatewayd
# ships inside os-server, and presync heals everything else, so a deeper
# structure check here would only force needless full reinstalls).
echo "[install-codex] declare verify hook for switch-runtime (codex + os-server)"
mkdir -p /usr/local/lib/os-runtimes/codex
cat >/usr/local/lib/os-runtimes/codex/verify <<'VERIFY'
#!/usr/bin/env bash
command -v codex >/dev/null 2>&1 && [ -x /usr/local/bin/os-server ]
VERIFY
chmod +x /usr/local/lib/os-runtimes/codex/verify

echo "[install-codex] done — codex gatewayd installed + started (codex.service)."
echo "[install-codex] ===== install finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
