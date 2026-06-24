#!/usr/bin/env bash
# runtime-hermes-presync — run by switch-runtime right before hermes starts (and
# once at the end of install.sh). It OWNS the device-side Hermes model config in
# config.yaml, in two layers:
#
#   1. STRUCTURE (static) — the provider wiring that routes Hermes at the device's
#      campaign-api brain via anthropic_messages. `hermes claw migrate` does NOT
#      carry it across, and a factory reset (`hermes setup --reset`) WIPES it back
#      to defaults. We assert it idempotently here so it self-heals on the next
#      switch — without this, only the first install.sh run ever wrote it, and
#      switch-runtime skips install.sh once hermes is installed, so a reset left
#      hermes pointed at a broken/default provider with no path to recover.
#
#   2. DYNAMIC (per-device) — the real llm_model / llm_base_url / llm_api_key and
#      channel tokens from /root/config/config.json, which override the defaults.
#
# This file is EMBEDDED IN os-server (internal/hermes/presync.sh) and materialized
# to /usr/local/bin/runtime-hermes-presync on every switch, so a plain os-server
# OTA refreshes it on disk — unlike a copy written by install.sh, which only re-runs
# on a first install / failed verify.
set -euo pipefail
CONFIG_JSON="/root/config/config.json"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"
CONFIG_YAML="$HERMES_DIR/config.yaml"
log() { echo "[hermes-presync] $*"; }

# ── 0. SKILLS (restore OpenClaw-imported skills if missing) ─────────────────────
# `hermes claw migrate` imports the device's OpenClaw skills into
# ~/.hermes/skills/openclaw-imports. It runs from install.sh on first install, but
# a factory reset (wipeHermesState) wipes that dir while leaving the OpenClaw
# workspace intact — and install.sh does NOT re-run on a later switch (verify
# passes), so the skills would never come back. Restore them here when the dir is
# empty/absent. GUARDED so a normal switch is a no-op (re-running every switch with
# --skill-conflict rename would pile up renamed duplicates). claw migrate has no
# skills-only preset (it also touches SOUL/MEMORY), but that is harmless: the Go
# persona migration runs afterwards (os-server boot) and re-writes SOUL/MEMORY
# cleanly, so only the skills it imported actually persist.
HERMES_BIN="${HERMES_BIN:-/usr/local/bin/hermes}"
IMPORTS_DIR="$HERMES_DIR/skills/openclaw-imports"
if [ ! -d "$IMPORTS_DIR" ] || [ -z "$(ls -A "$IMPORTS_DIR" 2>/dev/null)" ]; then
  if [ -x "$HERMES_BIN" ] && [ -d /root/.openclaw ]; then
    log "openclaw-imported skills missing — restoring via claw migrate"
    "$HERMES_BIN" claw migrate --preset full --overwrite --skill-conflict rename --yes --migrate-secrets \
      || log "WARN: claw migrate failed (non-fatal)"
  fi
fi

# yq is required for the structured config.yaml edits. install.sh installs it; if
# it is somehow absent we cannot safely patch — bail loudly (non-fatal to the
# switch: the caller treats presync failure as a warning).
if ! command -v yq >/dev/null 2>&1; then
  log "ERROR: yq not found — cannot ensure config.yaml structure" >&2
  exit 1
fi

# touch first so yq has a doc to edit on a fresh/absent config.yaml.
touch "$CONFIG_YAML"

# `hermes setup --reset` leaves top-level `.model` as an EMPTY STRING ('') and may
# leave `.custom_providers` as a scalar too — yq cannot index `.model.provider` /
# `.custom_providers[0]` into a scalar, so the structure assignment below would
# no-op and the config stays broken (`model: ''`). Coerce each to its expected
# container type FIRST, but only when it isn't already one, so a populated config
# is preserved.
[ "$(yq '.model | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.model = {}' "$CONFIG_YAML"
[ "$(yq '.custom_providers | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!seq" ] || yq -i '.custom_providers = []' "$CONFIG_YAML"

# ── 1. STRUCTURE (idempotent) ──────────────────────────────────────────────────
# Assert the static provider wiring. `// default` keeps any existing value (so a
# real llm_* already synced below is not stomped) and only fills it when missing.
# os-server sends a fixed request model ("hermes-agent", constants.go); .model
# .provider = custom:autonomous is what routes that bare model at the campaign-api
# custom provider, so this is the field that actually matters (the .default value
# is only a never-used fallback once a per-request model is sent).
log "ensure config.yaml model + custom_providers structure"
yq -i '
  .model.provider = "custom:autonomous"
  | .model.default = "Auto-AI"
  | .custom_providers[0].name     = "autonomous"
  | .custom_providers[0].key_env  = "AUTONOMOUS_API_KEY"
  | .custom_providers[0].api_mode = "anthropic_messages"
  | .custom_providers[0].base_url = (.custom_providers[0].base_url // "https://campaign-api.autonomous.ai/api/v1/ai")
' "$CONFIG_YAML"

# ── 2. DYNAMIC (config.json wins) ──────────────────────────────────────────────
# NOTE: .model.default is NOT synced from llm_model — that is the OpenClaw primary
# model (e.g. claude-opus-4-6), which is irrelevant to Hermes: os-server sends a
# fixed request model (constants.go Model) to the campaign-api custom provider, so
# the Hermes model default is the fixed "Auto-AI" alias set above, not the device's
# OpenClaw model. Only the provider endpoint + secrets are device-specific.
LLM_BASE_URL="$(jq -r '.llm_base_url // empty' "$CONFIG_JSON" 2>/dev/null || true)"
if [ -n "$LLM_BASE_URL" ]; then
  yq -i ".custom_providers[0].base_url = \"$LLM_BASE_URL\"" "$CONFIG_YAML"
  log "custom_providers[0].base_url = $LLM_BASE_URL"
fi

# .env secrets/IDs: config.json wins — UPSERT each non-empty config.json field
# into its Hermes env var (replace the existing line, or append if absent). Other
# variables are never touched, so whatever `hermes claw migrate` carried over
# from OpenClaw is kept as the fallback. Empty/missing config fields are skipped.
sync_env() {
  local key="$1" var="$2" val
  val="$(jq -r ".${key} // empty" "$CONFIG_JSON" 2>/dev/null || true)"
  [ -n "$val" ] || return 0
  sed -i "/^${var}=/d" "$ENV_FILE"
  # guard against gluing onto a previous line that lacks a trailing newline
  [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
  echo "${var}=${val}" >>"$ENV_FILE"
  log "${var} synced"
}

sync_env llm_api_key        AUTONOMOUS_API_KEY
sync_env telegram_bot_token TELEGRAM_BOT_TOKEN
sync_env telegram_user_id   TELEGRAM_ALLOWED_USERS
sync_env slack_bot_token    SLACK_BOT_TOKEN
sync_env slack_app_token    SLACK_APP_TOKEN
sync_env slack_user_id      SLACK_ALLOWED_USERS
sync_env discord_bot_token  DISCORD_BOT_TOKEN
sync_env discord_guild_id   DISCORD_GUILD_ID
sync_env discord_user_id    DISCORD_ALLOWED_USERS
sync_env whatsapp_user_id   WHATSAPP_ALLOWED_USERS
