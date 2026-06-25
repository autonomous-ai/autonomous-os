#!/usr/bin/env bash
# runtime-picoclaw-presync — run by switch-runtime right before picoclaw starts
# (and once at the end of install.sh). It OWNS the device-side PicoClaw model +
# channel config, split across PicoClaw's two config files:
#
#   /root/.picoclaw/config.json   — structure (agents.defaults, model_list,
#                                    channel_list: enabled/type/typing/placeholder
#                                    + non-secret settings)
#   /root/.picoclaw/.security.yml — secrets (model api_keys, channel tokens,
#                                    allow_from lists)
#
# Source of truth for the per-device secrets is the PROJECT config at
# /root/config/config.json (the same file hermes' presync reads) — NOT picoclaw's
# own config.json. config.json fields win; the picoclaw files only hold structure
# + whatever migrate carried over.
#
# Three layers, mirroring internal/hermes/presync.sh:
#   0. MIGRATE  — when workspace/skills is empty (first install OR after a factory
#      reset wiped it), run `picoclaw migrate --workspace-only --force` to carry
#      persona/memory/skills over from OpenClaw. PicoClaw has NO Go persona-migration
#      adapter (internal/agent/migrate_persona only knows openclaw+hermes), so this
#      hook is the ONLY thing that migrates persona/memory for picoclaw. --workspace-only
#      means migrate does NOT touch config.json (converting openclaw.json yields a
#      broken picoclaw config); config stays the onboard baseline that STRUCTURE/
#      DYNAMIC below assert model/channel/gateway on. Guarded so a normal switch is a
#      no-op (re-importing the workspace every switch would clobber local edits).
#   1. STRUCTURE (static, idempotent) — assert the provider wiring that routes
#      PicoClaw at the device's campaign-api brain via the anthropic-messages
#      provider. `picoclaw migrate` does NOT set it, and onboard/factory-reset
#      leave defaults, so we assert it here so it self-heals on the next switch.
#   2. DYNAMIC (per-device) — the real llm_api_key / llm_base_url and channel
#      tokens from /root/config/config.json, which override the defaults. Every
#      channel except pico (the native server gateway) is enabled ONLY when its
#      credentials are present in config.json.
#
# This file is EMBEDDED IN os-server (internal/picoclaw/presync.sh) and materialized
# to /usr/local/bin/runtime-picoclaw-presync on every switch, so a plain os-server
# OTA refreshes it on disk — unlike a copy written by install.sh, which only re-runs
# on a first install / failed verify.
set -euo pipefail

CONFIG_JSON="/root/config/config.json"          # device/project config (secret source of truth)
PICO_DIR="/root/.picoclaw"
PICO_CONFIG="$PICO_DIR/config.json"             # picoclaw's own config (structure)
PICO_SECURITY="$PICO_DIR/.security.yml"         # picoclaw's secrets
PICO_BIN="${PICO_BIN:-/usr/local/bin/picoclaw}"

# MUST equal internal/picoclaw/constants.go Token — os-server connects to the pico
# gateway with this bearer token, so the gateway must be seeded with the same value.
PICO_TOKEN="darren_pico_token"
# PicoClaw needs the OpenAI-style /v1 suffix on the campaign-api endpoint. NOTE this
# differs from hermes, which uses the bare .../api/v1/ai (no trailing /v1).
DEFAULT_API_BASE="https://campaign-api.autonomous.ai/api/v1/ai/v1"

log() { echo "[picoclaw-presync] $*"; }

for tool in jq yq; do
  command -v "$tool" >/dev/null 2>&1 || { log "ERROR: $tool not found — cannot patch picoclaw config" >&2; exit 1; }
done

# config.json must exist (install.sh runs `picoclaw onboard` first). Bail loudly if
# not — non-fatal to the switch (the caller treats presync failure as a warning).
[ -f "$PICO_CONFIG" ] || { log "ERROR: $PICO_CONFIG missing (onboard not run?)" >&2; exit 1; }
touch "$PICO_SECURITY"

# jq has no in-place flag; edit via temp + rename.
jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }
# read a field from the device config.json ("" when absent/empty).
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }

# ── 0. MIGRATE (restore persona/memory/skills from openclaw, once) ──────────────
# Gate on a sentinel marker, NOT on workspace/skills emptiness: PicoClaw ships
# built-in skills, so `workspace/skills` is ALWAYS non-empty (onboard seeds it) and
# an emptiness check would skip migrate forever. The marker lives under the picoclaw
# data dir, so a factory reset that wipes /root/.picoclaw clears it and migrate
# re-runs on the next switch. The marker is written ONLY after a clean migrate, so a
# failed migrate is retried next switch.
MIGRATE_MARKER="$PICO_DIR/.openclaw-migrated"
if [ ! -f "$MIGRATE_MARKER" ]; then
  if [ -x "$PICO_BIN" ] && [ -d /root/.openclaw ]; then
    log "no migration marker — migrating persona/memory/skills from openclaw"
    # stop openclaw first so migrate doesn't race its live on-disk state (retry 3x,
    # proceed regardless — migrate is non-fatal).
    for attempt in 1 2 3; do
      systemctl stop openclaw 2>/dev/null || true
      if ! systemctl is-active --quiet openclaw; then
        log "openclaw stopped (attempt ${attempt}/3)"; break
      fi
      [ "$attempt" -eq 3 ] && log "WARN: openclaw still active after 3 attempts — continuing anyway"
      sleep 1
    done
    # --workspace-only: migrate ONLY the workspace (persona/memory/skills), NOT
    # config.json — converting openclaw.json into a picoclaw config produces a broken
    # config (wrong model/channel/gateway shape). config.json therefore stays the
    # valid onboard baseline, and STRUCTURE/DYNAMIC below assert model/channel/gateway
    # on top. --force skips the interactive plan confirmation.
    if HOME=/root "$PICO_BIN" migrate --workspace-only --force; then
      WS="$PICO_DIR/workspace"
      OC_WS="/root/.openclaw/workspace"
      # 1) HEARTBEAT.md — copy openclaw's verbatim into the picoclaw workspace.
      if [ -f "$OC_WS/HEARTBEAT.md" ]; then
        cp -f "$OC_WS/HEARTBEAT.md" "$WS/HEARTBEAT.md"
        log "copied HEARTBEAT.md from openclaw"
      fi
      # 1b) KNOWLEDGE.md — accumulated learnings. openclaw seeds it from an embedded
      #     template (seedFileIfAbsent) then appends daily; `picoclaw migrate` skips it
      #     (like IDENTITY.md). Copy the device's living copy so picoclaw keeps the
      #     learnings instead of starting blank.
      if [ -f "$OC_WS/KNOWLEDGE.md" ]; then
        cp -f "$OC_WS/KNOWLEDGE.md" "$WS/KNOWLEDGE.md"
        log "copied KNOWLEDGE.md from openclaw"
      fi
      # 2) Drop AGENT.md so picoclaw runs the legacy AGENTS.md path, which is the only
      #    mode that reads IDENTITY.md. `picoclaw migrate` does NOT carry IDENTITY.md
      #    over, so copy openclaw's in manually.
      rm -f "$WS/AGENT.md"
      if [ -f "$OC_WS/IDENTITY.md" ]; then
        cp -f "$OC_WS/IDENTITY.md" "$WS/IDENTITY.md"
        log "copied IDENTITY.md from openclaw"
      fi
      touch "$MIGRATE_MARKER"
      log "migration complete — marker written ($MIGRATE_MARKER)"
    else
      log "WARN: picoclaw migrate failed (non-fatal) — will retry next switch"
    fi
  else
    log "no migration marker but openclaw absent or picoclaw binary missing — skipping migrate"
  fi
fi

# ── 1. STRUCTURE (idempotent) ───────────────────────────────────────────────────
# Route the default agent at the autonomous (campaign-api) provider. os-server
# sends a fixed model ("Auto-AI") at that provider; model_name "autonomous" is the
# alias resolved from model_list below. allow_read_outside_workspace lets skills
# reach device paths outside the workspace.
log "ensure agents.defaults model wiring"
jq_edit "$PICO_CONFIG" '
    .agents.defaults.restrict_to_workspace        = false
  | .agents.defaults.allow_read_outside_workspace = true
  | .agents.defaults.provider                     = "anthropic-messages"
  | .agents.defaults.model_name                   = "autonomous"
'

# Upsert the "autonomous" model_list entry — drop any existing copy, append a fresh
# one. Preserve an already-set api_base (DYNAMIC overrides it below from llm_base_url);
# fall back to the default endpoint when none exists yet.
log "ensure model_list autonomous entry"
jq_edit "$PICO_CONFIG" --arg ab "$DEFAULT_API_BASE" '
  ( [ (.model_list // [])[] | select(.model_name == "autonomous") | .api_base ]
    | map(select(. != null and . != "")) | .[0] ) as $existing
  | .model_list = ( (.model_list // []) | map(select(.model_name != "autonomous")) )
      + [ { model_name: "autonomous", provider: "anthropic-messages",
            model: "Auto-AI", api_base: ($existing // $ab) } ]
'

# Gateway server block — assert canonical host:port so it always matches constants.go
# WSURL (ws://127.0.0.1:18790/pico/ws/), regardless of the onboard default.
log "ensure gateway server block"
jq_edit "$PICO_CONFIG" '
  .gateway = { host: "localhost", port: 18790, hot_reload: false, log_level: "warn" }
'

# pico is the native server gateway — always enabled. Assert its structure (fill
# defaults only when missing so a customized config is preserved).
log "ensure channel_list.pico structure (always enabled)"
jq_edit "$PICO_CONFIG" '
    .channel_list.pico.enabled              = true
  | .channel_list.pico.type                 = "pico"
  | .channel_list.pico.reasoning_channel_id = (.channel_list.pico.reasoning_channel_id // "")
  | .channel_list.pico.group_trigger        = (.channel_list.pico.group_trigger // {})
  | .channel_list.pico.typing               = (.channel_list.pico.typing // {})
  | .channel_list.pico.placeholder          = (.channel_list.pico.placeholder // {enabled: true})
  | .channel_list.pico.settings             = (.channel_list.pico.settings // {
        max_connections: 100, ping_interval: 30, read_timeout: 60,
        streaming: {enabled: false}, write_timeout: 10, allow_token_query: true })
'

# Other channels: assert structure but DEFAULT enabled=false; §2 flips enabled=true
# only when the credentials exist in config.json.
ensure_channel_struct() {
  local ch="$1" type="$2"
  jq_edit "$PICO_CONFIG" --arg ch "$ch" --arg ty "$type" '
      .channel_list[$ch].type                 = $ty
    | .channel_list[$ch].enabled              = (.channel_list[$ch].enabled // false)
    | .channel_list[$ch].reasoning_channel_id = (.channel_list[$ch].reasoning_channel_id // "")
    | .channel_list[$ch].group_trigger        = (.channel_list[$ch].group_trigger // {})
    | .channel_list[$ch].typing               = (.channel_list[$ch].typing // {})
    | .channel_list[$ch].placeholder          = (.channel_list[$ch].placeholder // {enabled: false})
  '
}
log "ensure channel_list structure (telegram/discord/slack/whatsapp)"
ensure_channel_struct telegram telegram
ensure_channel_struct discord  discord
ensure_channel_struct slack    slack
ensure_channel_struct whatsapp whatsapp

# ── 2. DYNAMIC (config.json wins) ────────────────────────────────────────────────
# Helpers. enable_channel flips config.json; sec_* write to .security.yml via yq's
# strenv() so values are passed through the environment (no shell-quoting / yaml-
# injection risk). allow_from is a single id from config.json wrapped in an array.
#
# style="flow" on the settings map keeps the picoclaw-native inline shape
# `settings: { token: "...", allow_from: ["..."] }` instead of yq's default block
# style — picoclaw writes/expects flow there, and flow context also force-quotes the
# bot token (which contains a colon). Re-applied on every sec_* call (idempotent).
enable_channel() { jq_edit "$PICO_CONFIG" --arg ch "$1" '.channel_list[$ch].enabled = true'; }
sec_set() {
  CH="$1" K="$2" V="$3" yq -i \
    '.channel_list[strenv(CH)].settings[strenv(K)] = strenv(V) | .channel_list[strenv(CH)].settings style="flow"' \
    "$PICO_SECURITY"
}
sec_allow_from() {
  local ch="$1" id="$2"
  [ -n "$id" ] || return 0
  CH="$ch" ID="$id" yq -i \
    '.channel_list[strenv(CH)].settings.allow_from = [strenv(ID)] | .channel_list[strenv(CH)].settings style="flow"' \
    "$PICO_SECURITY"
}

# LLM endpoint: config.json llm_base_url wins; PicoClaw needs a trailing /v1.
LLM_BASE_URL="$(dev llm_base_url)"
if [ -n "$LLM_BASE_URL" ]; then
  base="${LLM_BASE_URL%/}"
  case "$base" in */v1) : ;; *) base="${base}/v1" ;; esac
  jq_edit "$PICO_CONFIG" --arg ab "$base" '
    .model_list = ((.model_list // []) | map(if .model_name == "autonomous" then .api_base = $ab else . end))
  '
  log "model_list[autonomous].api_base = $base"
fi

# LLM api key → .security.yml model_list."autonomous:0".api_keys.
LLM_API_KEY="$(dev llm_api_key)"
if [ -n "$LLM_API_KEY" ]; then
  KEY="$LLM_API_KEY" yq -i '.model_list["autonomous:0"].api_keys = [strenv(KEY)]' "$PICO_SECURITY"
  log "security model_list autonomous:0 api_keys synced"
fi

# pico bearer token (always) — must match constants.go Token.
PT="$PICO_TOKEN" yq -i '.channel_list.pico.settings.token = strenv(PT) | .channel_list.pico.settings style="flow"' "$PICO_SECURITY"
log "security channel_list.pico.settings.token synced"

# Telegram — enable when bot token present.
TG_TOKEN="$(dev telegram_bot_token)"; TG_USER="$(dev telegram_user_id)"
if [ -n "$TG_TOKEN" ]; then
  enable_channel telegram
  sec_set telegram token "$TG_TOKEN"
  sec_allow_from telegram "$TG_USER"
  log "telegram enabled + token synced"
else
  log "telegram: no telegram_bot_token in config.json — left disabled"
fi

# Discord — enable when bot token present (pico discord format: token + allow_from;
# discord_guild_id from config.json is not used by this channel).
DC_TOKEN="$(dev discord_bot_token)"; DC_USER="$(dev discord_user_id)"
if [ -n "$DC_TOKEN" ]; then
  enable_channel discord
  sec_set discord token "$DC_TOKEN"
  sec_allow_from discord "$DC_USER"
  log "discord enabled + token synced"
else
  log "discord: no discord_bot_token in config.json — left disabled"
fi

# Slack — needs BOTH bot + app token.
SL_BOT="$(dev slack_bot_token)"; SL_APP="$(dev slack_app_token)"; SL_USER="$(dev slack_user_id)"
if [ -n "$SL_BOT" ] && [ -n "$SL_APP" ]; then
  enable_channel slack
  sec_set slack bot_token "$SL_BOT"
  sec_set slack app_token "$SL_APP"
  sec_allow_from slack "$SL_USER"
  log "slack enabled + tokens synced"
else
  log "slack: missing slack_bot_token/slack_app_token in config.json — left disabled"
fi

# WhatsApp — native (whatsmeow) mode: no token, QR pairing on first gateway run,
# session persisted under the workspace. Enable when an allow_from id is present.
WA_USER="$(dev whatsapp_user_id)"
if [ -n "$WA_USER" ]; then
  enable_channel whatsapp
  jq_edit "$PICO_CONFIG" '
      .channel_list.whatsapp.settings.use_native         = true
    | .channel_list.whatsapp.settings.session_store_path = "/root/.picoclaw/workspace/whatsapp"
  '
  sec_allow_from whatsapp "$WA_USER"
  log "whatsapp enabled (native; QR pairing required on first run)"
else
  log "whatsapp: no whatsapp_user_id in config.json — left disabled"
fi

log "done — picoclaw model + channel config synced"
