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
#   §3 CHANNELS — nothing to configure: telegram + discord are device-owned
#      (telegram_poll.go / discord.go — os-server runs the receive loops
#      itself), so no channel plugin runs and CLAUDECODE_CHANNELS is not
#      written; stale ~/.claude/channels state from older presyncs is removed.
#
# The bridge itself is NOT materialized here anymore: it ships inside the
# os-server binary as the `os-server claudecode-gatewayd` subcommand
# (runtimes/claudecode/gatewayd — Go port of the former bridge.py), so a plain
# os-server OTA updates it. The gatewayd reads /root/.claudecode/.env itself,
# and EnsureOnboarding hash-gates the bridge restart on the files this script
# writes.
#
# This file is EMBEDDED IN os-server (runtimes/claudecode/presync.sh) and
# materialized to /usr/local/bin/runtime-claudecode-presync on every switch.
set -euo pipefail

# Env-overridable (with production defaults) so an off-device run and the tests
# can point at a temp dir. os-server passes all three explicitly (onboarding.go
# runPresync) — unset env is the board, byte for byte.
CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"   # device/project config (source of truth)
CC_DIR="${CLAUDECODE_HOME:-/root/.claudecode}"
WS_DIR="$CC_DIR/workspace"
ENV_FILE="$CC_DIR/.env"
CLAUDE_HOME="${CLAUDE_HOME:-/root/.claude}"

# Claude Code calls {ANTHROPIC_BASE_URL}/v1/messages — same anthropic-messages
# endpoint hermes uses, so the base has NO trailing /v1 (unlike picoclaw's
# OpenAI-style base).
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai"
DEFAULT_MODEL="Auto-AI"

log() { echo "[claudecode-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync claudecode config" >&2; exit 1; }

# Skills are USER-scoped (~/.claude/skills), not project-scoped: Claude Code
# resolves project skills from the session cwd, so a workspace-only install is
# invisible to the coding sessions the device spawns in other folders.
mkdir -p "$CLAUDE_HOME/skills" "$WS_DIR/.claude" "$WS_DIR/memory"

# read a field from the device config.json ("" when absent/empty).
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }
# jq has no in-place flag; edit via temp + rename.
jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }

# ── §1 SEEDS (headless flags, idempotent) ───────────────────────────────────────
# ~/.claude.json: skip the interactive first-run onboarding + accept the
# bypass-permissions warning — a headless device has no TTY to answer either.
log "seed headless flags in ~/.claude.json"
CLAUDE_JSON="$CLAUDE_HOME.json"   # ~/.claude.json — sibling of ~/.claude
[ -f "$CLAUDE_JSON" ] || echo '{}' >"$CLAUDE_JSON"
jq_edit "$CLAUDE_JSON" '
    .hasCompletedOnboarding          = true
  | .bypassPermissionsModeAccepted   = true
'

# workspace settings: trust .mcp.json project servers (os-server writes MCP
# connector entries there — runtimes/claudecode/mcp.go) without the interactive
# approval prompt.
log "seed workspace .claude/settings.json"
SETTINGS="$WS_DIR/.claude/settings.json"
mkdir -p "$WS_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
jq_edit "$SETTINGS" '.enableAllProjectMcpServers = true'

# ── §3 CHANNELS — none via plugins anymore. telegram AND discord are
# DEVICE-OWNED (telegram_poll.go getUpdates loop / discord.go discordgo
# session, mirroring codex): the native channel plugins proved undebuggable in
# the field (bun children, no journal logs, silent allowlist drops, silent
# death on restart races). They must NOT ride --channels, or plugin and
# os-server would compete for the same bot (Telegram 409s concurrent pollers;
# Discord would double-reply) — stale plugin state from older presyncs is
# removed here, and CLAUDECODE_CHANNELS is no longer written (the gatewayd
# omits --channels when the var is absent).
rm -rf "$CLAUDE_HOME/channels"

# ── §2 ENV (config.json wins) ───────────────────────────────────────────────────
# Two auth modes, decided by the claude login flow (runtimes/claudecode/login.go):
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
    echo "# Managed by runtime-claudecode-presync — do not edit (synced from $CONFIG_JSON)."
    echo "# Subscription auth: ANTHROPIC_* omitted on purpose (they outrank the OAuth login)."
    if [ -n "$OAUTH_TOKEN" ]; then
      echo "CLAUDE_CODE_OAUTH_TOKEN=$OAUTH_TOKEN"
    fi
    echo "DISABLE_AUTOUPDATER=1"
    echo "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
  } >"$ENV_FILE.tmp"
else
  LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
  # llm_base_url is the OpenAI-convention base (ends in /v1); Claude Code
  # appends /v1/messages itself, so keep the base /v1-less or the proxy sees
  # /v1/v1/messages → 404 (device-verified on campaign-api).
  LLM_BASE_URL="${LLM_BASE_URL%/v1}"
  LLM_API_KEY="$(dev llm_api_key)"
  LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"
  log "write $ENV_FILE (auth=api-key, base_url=$LLM_BASE_URL model=$LLM_MODEL key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
  cat >"$ENV_FILE.tmp" <<ENV
# Managed by runtime-claudecode-presync — do not edit (synced from $CONFIG_JSON).
ANTHROPIC_BASE_URL=$LLM_BASE_URL
# x-api-key ONLY: campaign-api 401s the Authorization: Bearer form, and claude
# prefers ANTHROPIC_AUTH_TOKEN (bearer) over ANTHROPIC_API_KEY when both are
# set — so the bearer var must stay unset (device-verified).
ANTHROPIC_API_KEY=$LLM_API_KEY
ANTHROPIC_MODEL=$LLM_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$LLM_MODEL
DISABLE_AUTOUPDATER=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV
  # Pre-approve the campaign key for INTERACTIVE claude (web CLI): Claude Code
  # identifies a custom ANTHROPIC_API_KEY by its last 20 chars and, unless that
  # id sits in ~/.claude.json customApiKeyResponses.approved, an interactive
  # session refuses the key and shows "Not logged in / run /login" (a stray
  # `rejected` entry from an earlier manual session bricks it entirely). The
  # headless gatewayd (--print) bypasses this gate, so only the web CLI needs
  # it. Seed approved / clear rejected so `claude` just works.
  if [ -n "$LLM_API_KEY" ]; then
    KEYID="${LLM_API_KEY: -20}"
    jq_edit "$CLAUDE_JSON" --arg k "$KEYID" '
        .customApiKeyResponses.approved = (((.customApiKeyResponses.approved // []) + [$k]) | unique)
      | .customApiKeyResponses.rejected = ((.customApiKeyResponses.rejected // []) | map(select(. != $k)))
    '
    log "pre-approved interactive API key (…$KEYID) in ~/.claude.json"
  fi
fi
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

# ── §4 CLI LOGIN SHELL ENV ───────────────────────────────────────────────────
# A bare `claude`/`codex` in an SSH/web-CLI login shell otherwise has no API key
# (the .env is only injected into the systemd service), so it prompts login and
# its /resume picker can't list sessions. Drop a profile.d snippet that sources
# the ACTIVE runtime's .env into INTERACTIVE login shells — runtime is resolved
# live from config.json so it stays correct across runtime switches without
# rewriting. Guarded to interactive shells only (no leak into scripts/cron).
write_cli_login_env() {
  cat >/etc/profile.d/agent-cli-env.sh <<'PROFILE'
# Managed by os-server runtime presync — do not edit.
case "$-" in *i*) ;; *) return 2>/dev/null || exit 0 ;; esac
_rt="$(sed -n 's/.*"agent_runtime"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /root/config/config.json 2>/dev/null | head -1)"
case "$_rt" in
  claudecode)
    if [ -f /root/.claudecode/.env ]; then set -a; . /root/.claudecode/.env; set +a; fi
    export IS_SANDBOX=1
    ;;
  codex)
    if [ -f /root/.codex/.env ]; then set -a; . /root/.codex/.env; set +a; fi
    export CODEX_HOME=/root/.codex
    ;;
esac
unset _rt
PROFILE
  chmod 0644 /etc/profile.d/agent-cli-env.sh
}
# Root-only: off-device this just printed "No such file or directory" to stderr
# every run (never fatal — the `&&` exempts it from `set -e`).
if [ "$(id -u)" -eq 0 ]; then
  write_cli_login_env && log "wrote /etc/profile.d/agent-cli-env.sh (interactive CLI auto-login)"
else
  log "skip /etc/profile.d/agent-cli-env.sh (not root — off-device run)"
fi

# ── §5 UNIFIED SESSION PICKER (`claude-sessions`) ────────────────────────────
# Claude's interactive /resume picker excludes headless (--print) sessions by
# design, so Telegram-created sessions never appear in it. `claude-sessions` is
# the device picker that lists EVERY session for the current folder (same
# discovery the Telegram feature uses) and resumes the picked one via
# `claude --resume <id>` — implemented as the `os-server claude-sessions`
# subcommand (cmd/os-server/cc.go); this wrapper just sudo-reexecs into it
# (sessions live under /root).
write_session_picker() {
  cat >/usr/local/bin/claude-sessions <<'PICKER'
#!/bin/sh
# Managed by os-server runtime presync — do not edit.
# Unified claude coding-session picker: `claude-sessions` in a folder lists its
# sessions (terminal- AND Telegram-created) and resumes the one you pick.
[ "$(id -u)" -eq 0 ] || exec sudo /usr/local/bin/claude-sessions "$@"
exec /usr/local/bin/os-server claude-sessions "$@"
PICKER
  chmod 0755 /usr/local/bin/claude-sessions
  # Remove the picker's earlier `cc` name — only if it is OUR managed wrapper
  # (never clobber a real C-compiler cc that may sit there on other systems).
  if grep -q "Managed by os-server runtime presync" /usr/local/bin/cc 2>/dev/null; then
    rm -f /usr/local/bin/cc
  fi
}
# Root-only, and this guard matters: Homebrew leaves /usr/local/bin
# group-writable, so off-device this SUCCEEDS and drops a sudo-reexec wrapper
# into the dev's PATH pointing at an os-server that isn't there.
if [ "$(id -u)" -eq 0 ]; then
  write_session_picker && log "wrote /usr/local/bin/claude-sessions (unified session picker)"
else
  log "skip /usr/local/bin/claude-sessions (not root — off-device run)"
fi

log "done — claudecode env + channel config synced"
