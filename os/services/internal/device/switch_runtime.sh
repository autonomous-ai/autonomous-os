#!/usr/bin/env bash
# switch-runtime <new> [old] — generic agentic-backend switcher.
#
# Backend-agnostic: knows NOTHING about hermes/openclaw/picoclaw specifically.
# Everything backend-specific lives outside this script:
#   - a backend "X" is installed by its install.sh, which must create a systemd
#     unit + seed its own config. The unit defaults to X.service, but an
#     installer whose unit has a different name (e.g. hermes →
#     hermes-gateway.service, created by `hermes gateway install --system`)
#     declares it in /usr/local/lib/os-runtimes/X/service (one line, no .service
#     suffix); this script reads that to stay name-agnostic. The installer is
#     found LOCALLY first at /usr/local/lib/os-runtimes/X/install.sh (materialized
#     by os-server from a binary-embedded copy — works fully offline), else
#     fetched from ${RUNTIMES_BASE_URL}/X/install.sh on the CDN;
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

# Resolve the systemd unit name a backend uses. Defaults to the runtime name,
# but an installer may declare a different unit in
# /usr/local/lib/os-runtimes/<name>/service (one line, no .service suffix) — e.g.
# hermes → hermes-gateway. Keeps this script name-agnostic.
unit_for() {
  local f="/usr/local/lib/os-runtimes/${1}/service" name
  if [ -r "$f" ] && name="$(tr -d '[:space:]' <"$f")" && [ -n "$name" ]; then
    echo "$name"
  else
    echo "$1"
  fi
}

# stop_unit_retry <unit> — stop + disable a unit, retrying up to 3 times if it is
# still active afterwards. Always returns 0 (proceeds) so a stubborn old runtime
# never blocks the switch; logs a warning if it never went inactive.
stop_unit_retry() {
  local unit="$1" attempt
  for attempt in 1 2 3; do
    systemctl disable --now "${unit}.service" 2>/dev/null || true
    if ! systemctl is-active --quiet "${unit}.service"; then
      log "stopped ${unit}.service (attempt ${attempt}/3)"
      return 0
    fi
    log "WARN: ${unit}.service still active after attempt ${attempt}/3"
    sleep 1
  done
  log "WARN: ${unit}.service still active after 3 attempts — continuing anyway"
  return 0
}

# Roll back to OLD if the switch fails before we reach the os-server restart. The
# hermes installer stops openclaw EARLY (before it finishes installing), so a
# mid-switch failure would otherwise leave the device with NO backend running
# while config.agent_runtime (persisted by os-server before this ran) already
# points at the half-installed NEW one — dead on the next reboot. This trap
# restarts OLD and reverts config.agent_runtime so the device stays fully
# consistent on OLD. Disarmed (switched=1) once NEW is up and OLD is stopped.
CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"
switched=0

rollback() {
  [ "$switched" = 1 ] && return 0
  [ -n "$OLD" ] && [ "$OLD" != "$NEW" ] || return 0
  log "ERROR: switch to $NEW failed — rolling back to $OLD"
  if systemctl enable --now "$(unit_for "$OLD").service" 2>/dev/null; then
    log "restored $OLD"
  else
    log "WARN: could not restore $OLD"
  fi
  if command -v jq >/dev/null 2>&1 && [ -w "$CONFIG_JSON" ]; then
    local tmp
    tmp="$(mktemp)"
    if jq --arg r "$OLD" '.agent_runtime = $r' "$CONFIG_JSON" >"$tmp" && mv "$tmp" "$CONFIG_JSON"; then
      log "reverted config.agent_runtime → $OLD"
    else
      log "WARN: could not revert config.agent_runtime"
      rm -f "$tmp" 2>/dev/null || true
    fi
  fi
}
trap rollback EXIT

# 1. Ensure the target backend is installed. Its installer owns creating its
#    unit (and declaring a non-default unit name; see unit_for). (openclaw.service
#    is baked by setup.sh, so this is skipped for openclaw — uniform path, no
#    special-case.) Prefer the binary-embedded installer materialized by os-server
#    (offline); fall back to the CDN.
NEW_UNIT="$(unit_for "$NEW")"
if ! unit_exists "$NEW_UNIT"; then
  LOCAL_INSTALLER="/usr/local/lib/os-runtimes/${NEW}/install.sh"
  if [ -x "$LOCAL_INSTALLER" ]; then
    log "$NEW not installed — running embedded $LOCAL_INSTALLER"
    "$LOCAL_INSTALLER"
  else
    log "$NEW not installed — fetching ${RUNTIMES_BASE_URL}/${NEW}/install.sh"
    curl -fsSL "${RUNTIMES_BASE_URL}/${NEW}/install.sh" | bash
  fi
  NEW_UNIT="$(unit_for "$NEW")" # re-read: the installer may have just declared it
fi
if ! unit_exists "$NEW_UNIT"; then
  log "ERROR: ${NEW_UNIT}.service still absent after install — aborting (not restarting os-server)"
  exit 1
fi

# 2. Optional backend pre-start hook (generic: run it if present + executable).
HOOK="/usr/local/bin/runtime-${NEW}-presync"
if [ -x "$HOOK" ]; then
  log "running pre-start hook $HOOK"
  "$HOOK" || log "WARN: $HOOK failed (non-fatal)"
fi

# 3. Toggle units: start the new one, stop the old one (os-server tells us which).
log "starting $NEW ($NEW_UNIT.service)"
systemctl enable --now "${NEW_UNIT}.service"
# Verify NEW actually came up before we stop OLD. `enable --now` returning 0 only
# means systemd attempted the start; a unit that crashes immediately can still
# exit 0 here. If NEW isn't active, abort — the EXIT trap rolls back to OLD.
if ! systemctl is-active --quiet "${NEW_UNIT}.service"; then
  log "ERROR: ${NEW_UNIT}.service not active after start — aborting"
  exit 1
fi
if [ -n "$OLD" ] && [ "$OLD" != "$NEW" ]; then
  OLD_UNIT="$(unit_for "$OLD")"
  log "stopping $OLD ($OLD_UNIT.service)"
  stop_unit_retry "$OLD_UNIT"
fi

# 4. Restart os-server so internal/agent/factory.go re-resolves the gateway.
# Past the point of no return: NEW is up and OLD is stopped, so disarm the
# rollback trap (the os-server restart below does not kill us — we run in our own
# transient unit).
switched=1
log "restarting os-server"
systemctl restart os-server
log "done — now running $NEW"
