#!/usr/bin/env bash
# runtime-claudecode-presync — run by switch-runtime right before claudecode
# starts, once at the end of install.sh, and by EnsureOnboarding on every
# os-server boot / config change (hermes-style). It OWNS everything stateful:
#
#   §1 SEEDS    — headless flags in /root/.claude.json (skip interactive
#      onboarding) + workspace .claude/settings.json (trust .mcp.json entries).
#   §2 ENV      — /root/.claudecode/.env. Auth mode is decided here: claude.ai
#      SUBSCRIPTION (claude_code_oauth_token from the login flow, or
#      ~/.claude/.credentials.json on disk → CLAUDE_CODE_OAUTH_TOKEN, no
#      ANTHROPIC_*) vs API-KEY (ANTHROPIC_* from llm_api_key / llm_base_url /
#      llm_model — the same source hermes/picoclaw presync reads).
#   §3 CHANNELS — Claude Code's native channel plugin config (telegram +
#      discord): ~/.claude/channels/<ch>/.env (bot token) + access.json
#      (dmPolicy allowlist seeded from the owner's user id — replaces the
#      interactive /telegram:access | /discord:access pairing, which a headless
#      device cannot run), and the CLAUDECODE_CHANNELS launch flag the bridge
#      passes to `claude --channels`.
#
# The bridge itself is NOT materialized here anymore: it ships inside the
# os-server binary as the `os-server claudecode-gatewayd` subcommand
# (internal/claudecode/gatewayd — Go port of the former bridge.py), so a plain
# os-server OTA updates it. The gatewayd reads /root/.claudecode/.env itself
# (including CLAUDECODE_CHANNELS written in §2), and EnsureOnboarding
# hash-gates the bridge restart on the files this script writes.
#
# This file is EMBEDDED IN os-server (internal/claudecode/presync.sh) and
# materialized to /usr/local/bin/runtime-claudecode-presync on every switch.
set -euo pipefail

CONFIG_JSON="/root/config/config.json"          # device/project config (source of truth)
CC_DIR="/root/.claudecode"
WS_DIR="$CC_DIR/workspace"
ENV_FILE="$CC_DIR/.env"
CLAUDE_HOME="/root/.claude"

# Claude Code calls {ANTHROPIC_BASE_URL}/v1/messages — same anthropic-messages
# endpoint hermes uses, so the base has NO trailing /v1 (unlike picoclaw's
# OpenAI-style base).
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai"
DEFAULT_MODEL="Auto-AI"

log() { echo "[claudecode-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync claudecode config" >&2; exit 1; }

mkdir -p "$WS_DIR/.claude/skills" "$WS_DIR/memory"

# read a field from the device config.json ("" when absent/empty).
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }
# jq has no in-place flag; edit via temp + rename.
jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }

# ── §1 SEEDS (headless flags, idempotent) ───────────────────────────────────────
# ~/.claude.json: skip the interactive first-run onboarding + accept the
# bypass-permissions warning — a headless device has no TTY to answer either.
log "seed headless flags in ~/.claude.json"
CLAUDE_JSON="/root/.claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' >"$CLAUDE_JSON"
jq_edit "$CLAUDE_JSON" '
    .hasCompletedOnboarding          = true
  | .bypassPermissionsModeAccepted   = true
'

# workspace settings: trust .mcp.json project servers (os-server writes MCP
# connector entries there — internal/claudecode/mcp.go) without the interactive
# approval prompt.
log "seed workspace .claude/settings.json"
SETTINGS="$WS_DIR/.claude/settings.json"
mkdir -p "$WS_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
jq_edit "$SETTINGS" '.enableAllProjectMcpServers = true'

# ── §3 CHANNELS (computed before §2 so the .env write includes the launch flags)
# Claude Code runs telegram + discord natively via its channel plugins: the
# bridge launches `claude --channels <plugins>`, each plugin polls its Bot API
# with the token in ~/.claude/channels/<ch>/.env, and access.json gates senders.
# We seed dmPolicy=allowlist with the configured owner id — the interactive
# pairing flow (/telegram:access pair, /discord:access pair) needs a terminal
# this device does not have. Both plugins share the same access.json schema.
CHANNELS=""

# sync_channel <name> <token-env-var> <token> <owner-id> — writes the plugin's
# .env + allowlist and appends the plugin to the --channels launch list.
sync_channel() {
  local ch="$1" var="$2" token="$3" user="$4"
  local dir="$CLAUDE_HOME/channels/$ch"
  if [ -z "$token" ]; then
    log "$ch: no bot token in config.json — channel left disabled"
    return 0
  fi
  mkdir -p "$dir"
  umask 077
  printf '%s=%s\n' "$var" "$token" >"$dir/.env"
  umask 022
  local access="$dir/access.json"
  [ -f "$access" ] || echo '{}' >"$access"
  if [ -n "$user" ]; then
    jq_edit "$access" --arg id "$user" '
        .dmPolicy  = "allowlist"
      | .allowFrom = (((.allowFrom // []) + [$id]) | unique)
    '
    log "$ch enabled — token synced, allowlist seeded ($user)"
  else
    # No owner id → leave the plugin's default pairing policy; inbound stays
    # gated (headless pairing is not possible) until the user id is configured.
    log "$ch token set but owner user id missing — allowlist NOT seeded (inbound gated on pairing)"
  fi
  CHANNELS="${CHANNELS:+$CHANNELS }plugin:${ch}@claude-plugins-official"
}

sync_channel telegram TELEGRAM_BOT_TOKEN "$(dev telegram_bot_token)" "$(dev telegram_user_id)"
sync_channel discord  DISCORD_BOT_TOKEN  "$(dev discord_bot_token)"  "$(dev discord_user_id)"

# ── §2 ENV (config.json wins) ───────────────────────────────────────────────────
# Two auth modes, decided by the claude login flow (internal/claudecode/login.go):
#
#   subscription — claude_code_oauth_token set in config.json (or the CLI saved
#     ~/.claude/.credentials.json): inject CLAUDE_CODE_OAUTH_TOKEN and OMIT every
#     ANTHROPIC_* var — API-key vars OUTRANK OAuth in Claude Code's credential
#     precedence, so leaving them set would silently override the login.
#   api-key — default: campaign-api via llm_* from config.json.
OAUTH_TOKEN="$(dev claude_code_oauth_token)"
umask 077
if [ -n "$OAUTH_TOKEN" ] || [ -s "$CLAUDE_HOME/.credentials.json" ]; then
  log "write $ENV_FILE (auth=claude.ai subscription, token=$( [ -n "$OAUTH_TOKEN" ] && echo config || echo credentials.json ))"
  {
    echo "# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json)."
    echo "# Subscription auth: ANTHROPIC_* omitted on purpose (they outrank the OAuth login)."
    if [ -n "$OAUTH_TOKEN" ]; then
      echo "CLAUDE_CODE_OAUTH_TOKEN=$OAUTH_TOKEN"
    fi
    echo "DISABLE_AUTOUPDATER=1"
    echo "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
    echo "CLAUDECODE_CHANNELS=$CHANNELS"
  } >"$ENV_FILE.tmp"
else
  LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
  LLM_API_KEY="$(dev llm_api_key)"
  LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"
  log "write $ENV_FILE (auth=api-key, base_url=$LLM_BASE_URL model=$LLM_MODEL key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
  cat >"$ENV_FILE.tmp" <<ENV
# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json).
ANTHROPIC_BASE_URL=$LLM_BASE_URL
# Both auth vars carry llm_api_key: claude sends x-api-key from ANTHROPIC_API_KEY
# and Authorization: Bearer from ANTHROPIC_AUTH_TOKEN — campaign-api accepts the
# bearer form; setting both keeps either proxy convention working.
ANTHROPIC_API_KEY=$LLM_API_KEY
ANTHROPIC_AUTH_TOKEN=$LLM_API_KEY
ANTHROPIC_MODEL=$LLM_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$LLM_MODEL
DISABLE_AUTOUPDATER=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
CLAUDECODE_CHANNELS=$CHANNELS
ENV
fi
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

log "done — claudecode env + channel config synced"
