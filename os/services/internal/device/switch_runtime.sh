#!/usr/bin/env bash
# switch-runtime <new> [old] — generic agentic-backend switcher.
#
# Backend-agnostic: knows NOTHING about hermes/openclaw/picoclaw specifically.
# Everything backend-specific lives outside this script:
#   - a backend "X" is installed by its install.sh, which must create an
#     X.service systemd unit + seed its own config. The installer is found
#     LOCALLY first at /usr/local/lib/os-runtimes/X/install.sh (materialized by
#     os-server from a binary-embedded copy — works fully offline), else fetched
#     from ${RUNTIMES_BASE_URL}/X/install.sh on the CDN;
#   - an optional pre-start hook  /usr/local/bin/runtime-X-presync  (dropped by
#     that installer) runs right before X starts — e.g. hermes syncs llm_* from
#     config.json into its own config there.
#
# So adding a new backend = ship its install.sh (next to that backend's
# implementation) and publish it to ${RUNTIMES_BASE_URL}/<name>/install.sh. No
# change to this script, to os-server, or to the imager — ever.
#
# This file is EMBEDDED IN os-server (internal/device/switch_runtime.sh) and
# written to /usr/local/bin/switch-runtime on demand, so it is versioned and
# OTA-updated together with the binary.
#
# os-server invokes it (with the previous runtime as $2 so we know what to stop):
#   systemd-run --collect --unit=os-runtime-switch \
#     /usr/local/bin/switch-runtime <new> <old>
# in its own transient unit, so the os-server restart at the end can't kill it.
#
# config.agent_runtime itself is persisted by os-server BEFORE this runs; this
# script only owns the systemd + backend-install side effects.
set -euo pipefail

NEW="${1:-}"
OLD="${2:-}"
RUNTIMES_BASE_URL="${RUNTIMES_BASE_URL:-https://cdn.autonomous.ai/os/runtimes}"

log() { echo "[switch-runtime] $*"; }

[ -n "$NEW" ] || { echo "Usage: switch-runtime <new> [old]" >&2; exit 1; }

unit_exists() { systemctl cat "${1}.service" >/dev/null 2>&1; }

# 1. Ensure the target backend is installed. Its installer owns creating
#    <NEW>.service. (openclaw.service is baked by setup.sh, so this is skipped
#    for openclaw — uniform path, no special-case.) Prefer the binary-embedded
#    installer materialized by os-server (offline); fall back to the CDN.
if ! unit_exists "$NEW"; then
  LOCAL_INSTALLER="/usr/local/lib/os-runtimes/${NEW}/install.sh"
  if [ -x "$LOCAL_INSTALLER" ]; then
    log "$NEW not installed — running embedded $LOCAL_INSTALLER"
    "$LOCAL_INSTALLER"
  else
    log "$NEW not installed — fetching ${RUNTIMES_BASE_URL}/${NEW}/install.sh"
    curl -fsSL "${RUNTIMES_BASE_URL}/${NEW}/install.sh" | bash
  fi
fi
if ! unit_exists "$NEW"; then
  log "ERROR: ${NEW}.service still absent after install — aborting (not restarting os-server)"
  exit 1
fi

# 2. Optional backend pre-start hook (generic: run it if present + executable).
HOOK="/usr/local/bin/runtime-${NEW}-presync"
if [ -x "$HOOK" ]; then
  log "running pre-start hook $HOOK"
  "$HOOK" || log "WARN: $HOOK failed (non-fatal)"
fi

# 3. Toggle units: start the new one, stop the old one (os-server tells us which).
log "starting $NEW"
systemctl enable --now "${NEW}.service"
if [ -n "$OLD" ] && [ "$OLD" != "$NEW" ]; then
  log "stopping $OLD"
  systemctl disable --now "${OLD}.service" 2>/dev/null || true
fi

# 4. Restart os-server so internal/agent/factory.go re-resolves the gateway.
log "restarting os-server"
systemctl restart os-server
log "done — now running $NEW"
