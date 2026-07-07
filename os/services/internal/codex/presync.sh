#!/usr/bin/env bash
# runtime-codex-presync — run by switch-runtime right before codex starts, once
# at the end of install.sh, and by EnsureOnboarding on every os-server boot /
# config change (hermes-style). It does NOT restart codex.service — the Go side
# owns unit restarts (EnsureOnboarding hash-gates .env + config.toml around
# this run). The bridge itself ships inside the os-server binary (`os-server
# codex-gatewayd`) — nothing to materialize here. It OWNS everything stateful:
#
#   §1 MIGRATE — one-time persona/memory/skills copy from the openclaw
#      workspace (marker /root/.codex/.openclaw-migrated; a factory reset that
#      wipes /root/.codex clears it so migrate re-runs on the next switch).
#   §2 CONFIG  — ~/.codex/config.toml model-provider wiring from config.json
#      llm_* (guarded merge: os-server-owned [mcp_servers] tail preserved).
#   §3 ENV     — /root/.codex/.env (systemd EnvironmentFile): gatewayd
#      token/port + OPENAI_API_KEY from llm_api_key.
#
# This file is EMBEDDED IN os-server (internal/codex/presync.sh) and
# materialized to /usr/local/bin/runtime-codex-presync on every switch.
set -euo pipefail

# Env-overridable (with production defaults) so tests can point at a temp dir.
CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"   # device/project config (source of truth)
CODEX_DIR="${CODEX_DIR:-/root/.codex}"
WS_DIR="$CODEX_DIR/workspace"
ENV_FILE="$CODEX_DIR/.env"
CODEX_CONFIG="$CODEX_DIR/config.toml"

# Codex speaks the OpenAI Responses API only (the chat-completions wire was
# removed upstream ~2/2026): it appends /responses to base_url, so the base
# needs the OpenAI-style /v1 suffix (like picoclaw's base, unlike claudecode's).
# ⚠️ VERIFY ON DEVICE: campaign-api must serve {base}/responses.
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai/v1"
DEFAULT_MODEL="Auto-AI"

log() { echo "[codex-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync codex config" >&2; exit 1; }

mkdir -p "$WS_DIR" "$CODEX_DIR/attachments"

# read a field from the device config.json ("" when absent/empty), newline-safe
# for the KEY=value .env format below.
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null | tr -d '\n\r' || true; }

# ── §1 MIGRATE (restore persona/memory/skills from openclaw, once) ──────────────
# Gate on a sentinel marker under the codex data dir, so a factory reset that
# wipes /root/.codex clears it and migrate re-runs on the next switch. The
# marker is written ONLY after a clean copy, so a failed migrate is retried on
# the next run. Non-fatal: a device without openclaw just skips this.
MIGRATE_MARKER="$CODEX_DIR/.openclaw-migrated"
OC_WS="/root/.openclaw/workspace"
if [ ! -f "$MIGRATE_MARKER" ] && [ -d "$OC_WS" ]; then
  log "no migration marker — migrating persona/memory/skills from openclaw"
  # stop openclaw first so the copy doesn't race its live on-disk state (retry
  # 3x, proceed regardless — migrate is non-fatal).
  for attempt in 1 2 3; do
    systemctl stop openclaw 2>/dev/null || true
    if ! systemctl is-active --quiet openclaw; then
      log "openclaw stopped (attempt ${attempt}/3)"; break
    fi
    [ "$attempt" -eq 3 ] && log "WARN: openclaw still active after 3 attempts — continuing anyway"
    sleep 1
  done
  if (
    set -e
    # Persona files — copy openclaw's living copies verbatim (overwrite).
    # AGENTS.md included: codex reads AGENTS.md natively, so openclaw's is
    # directly usable (the Go onboarding re-injects the OS block anyway).
    for f in IDENTITY.md SOUL.md KNOWLEDGE.md HEARTBEAT.md MEMORY.md USER.md AGENTS.md; do
      if [ -f "$OC_WS/$f" ]; then
        cp -f "$OC_WS/$f" "$WS_DIR/$f"
        log "copied $f from openclaw"
      fi
    done
    # memory/ + skills/ — only when the destination is absent, so a re-run
    # never clobbers local edits.
    for d in memory skills; do
      if [ -d "$OC_WS/$d" ] && [ ! -d "$WS_DIR/$d" ]; then
        cp -a "$OC_WS/$d" "$WS_DIR/$d" && log "copied $d/ from openclaw"
      fi
    done
  ); then
    touch "$MIGRATE_MARKER"
    log "openclaw migration complete (marker written)"
  else
    log "WARN: openclaw migration failed — will retry on next presync run"
  fi
fi

# ── §2 CONFIG (~/.codex/config.toml, guarded merge) ─────────────────────────────
# The head (model + provider wiring) is regenerated from config.json every run.
# The [mcp_servers.*] tables are preserved verbatim: os-server
# (internal/codex/mcp.go) owns those entries. IMPORTANT: the Go writers
# (mcp.go, migrate_config/runtime_codex.go) round-trip config.toml through
# go-toml v2 maps, which SORTS tables alphabetically — [mcp_servers] can end up
# BEFORE [model_providers]. A naive "tail from the first [mcp_servers line to
# EOF" grab would then also swallow [model_providers.autonomous] and duplicate
# it under the regenerated head (unparseable TOML). So the extraction below is
# position-independent: it captures ONLY mcp_servers tables wherever they
# appear, stopping at the next non-mcp top-level table header.
LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
# Normalize: strip trailing slash, append /v1 unless the base already ends in it.
LLM_BASE_URL="${LLM_BASE_URL%/}"
case "$LLM_BASE_URL" in
  */v1) ;;
  *) LLM_BASE_URL="$LLM_BASE_URL/v1" ;;
esac
LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"

log "write $CODEX_CONFIG (model=$LLM_MODEL base_url=$LLM_BASE_URL)"
cat >"$CODEX_CONFIG.tmp" <<TOML
# Managed by runtime-codex-presync — head regenerated from /root/config/config.json.
# All [mcp_servers.*] tables are preserved wherever they appear
# (os-server internal/codex/mcp.go owns those entries).
model = "$LLM_MODEL"
model_provider = "autonomous"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.autonomous]
name = "Autonomous campaign-api"
# Codex appends /responses to base_url (Responses API only — the chat wire was
# removed upstream ~2/2026). ⚠️ VERIFY ON DEVICE: campaign-api must serve
# {base}/responses.
base_url = "$LLM_BASE_URL"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
TOML
# Re-append the preserved [mcp_servers.*] tables. Position-independent (see
# comment above): capture starts at any ^[mcp_servers header (incl. nested
# [mcp_servers.x.y] subtables) and stops at the next top-level table header
# that is NOT an mcp_servers table; supports multiple disjoint occurrences.
if [ -f "$CODEX_CONFIG" ]; then
  MCP_BLOCK="$(awk '
    /^\[mcp_servers[].]/ { capture=1; print; next }
    capture && /^\[/     { capture=0 }
    capture              { print }
  ' "$CODEX_CONFIG")"
  if [ -n "$MCP_BLOCK" ]; then
    log "preserving existing [mcp_servers] tables"
    printf '\n%s\n' "$MCP_BLOCK" >>"$CODEX_CONFIG.tmp"
  fi
fi
mv "$CODEX_CONFIG.tmp" "$CODEX_CONFIG"

# ── §3 ENV (config.json wins) ───────────────────────────────────────────────────
# systemd EnvironmentFile format: plain KEY=value lines, no quoting.
LLM_API_KEY="$(dev llm_api_key)"
log "write $ENV_FILE (key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
umask 077
{
  echo "# Managed by runtime-codex-presync — do not edit (synced from /root/config/config.json)."
  # CODEX_WS_TOKEN MUST match internal/codex/constants.go Token; read by
  # `os-server codex-gatewayd` (gatewayd package).
  echo "CODEX_WS_TOKEN=autonomous_codex_token"
  echo "CODEX_PORT=18792"
  echo "CODEX_HOME=$CODEX_DIR"
  echo "CODEX_WORKSPACE=$WS_DIR"
  if [ -n "$LLM_API_KEY" ]; then
    echo "OPENAI_API_KEY=$LLM_API_KEY"
  fi
} >"$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

# Channels: codex has no inbound channel — the Codex CLI has no channel layer
# (unlike picoclaw, whose own config gets telegram enabled by its presync) and
# os-server runs no Telegram receive loop. Nothing to write here; outbound-only
# alerts go through os-server's TelegramSender. See TODO(codex-telegram) in
# internal/codex/channels.go.
log "channels: none supported under codex — nothing to sync"

log "done — codex config.toml + env synced"
