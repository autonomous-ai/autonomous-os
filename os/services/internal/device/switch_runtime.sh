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
# os-server invokes it (with the previous runtime as $2 so we know what to stop)
# in its own transient unit AND BLOCKS on the exit code:
#   systemd-run --collect --wait --unit=os-runtime-switch \
#     /usr/local/bin/switch-runtime <new> <old>
# --wait lets os-server learn whether the switch landed or rolled back, so it can
# ack the real hermes.setup / picoclaw.setup outcome. This script does NOT restart
# os-server (it used to); os-server restarts itself AFTER acking, which is also
# what makes factory.go re-resolve the gateway.
#
# config.agent_runtime itself is persisted by os-server BEFORE this runs; this
# script only owns the systemd + backend-install side effects (and, on failure,
# rolling those + config.agent_runtime back).
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

# Roll back to OLD if the switch fails before NEW is confirmed up. The hermes
# installer stops openclaw EARLY (before it finishes installing), so a mid-switch
# failure would otherwise leave the device with NO backend running. This trap
# restarts OLD so the device stays consistent on OLD. config.agent_runtime needs no
# revert here: os-server persists NEW only after we exit 0, so on any failure config
# is still OLD on disk. Disarmed (switched=1) once NEW is up and OLD is stopped.
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
}
trap rollback EXIT

# install_new — run the target backend's installer (binary-embedded copy first,
# CDN fallback) and re-resolve its unit name (the installer may have just declared
# a non-default one). Used both for a first-time install and to self-heal an
# orphaned/half-installed backend. Mutates the global NEW_UNIT.
install_new() {
  local local_installer="/usr/local/lib/os-runtimes/${NEW}/install.sh"
  if [ -x "$local_installer" ]; then
    log "running embedded installer $local_installer"
    "$local_installer"
  else
    log "fetching installer ${RUNTIMES_BASE_URL}/${NEW}/install.sh"
    curl -fsSL "${RUNTIMES_BASE_URL}/${NEW}/install.sh" | bash
  fi
  NEW_UNIT="$(unit_for "$NEW")"
}

# backend_installed — true only when the backend is BOTH declared (unit file
# present) AND actually usable. Usability is verified via an optional installer-
# provided hook at /usr/local/lib/os-runtimes/<name>/verify (e.g. hermes writes
# one that runs `command -v hermes`). This catches the orphaned-unit trap: a stale
# <name>.service left behind while the backend's binary is gone/broken — the old
# unit-presence-only gate treated that as "installed" and NEVER reinstalled, so a
# half-removed backend could never recover. Backends without a verify hook (e.g.
# openclaw, whose unit is baked by setup.sh) fall back to unit-presence alone.
backend_installed() {
  unit_exists "$NEW_UNIT" || return 1
  local verify="/usr/local/lib/os-runtimes/${NEW}/verify"
  [ -x "$verify" ] || return 0
  "$verify" >/dev/null 2>&1
}

# run_presync — optional pre-start hook dropped by the installer (e.g. hermes
# syncs llm_* from config.json into its own config here). No-op if absent.
HOOK="/usr/local/bin/runtime-${NEW}-presync"
run_presync() {
  [ -x "$HOOK" ] || return 0
  log "running pre-start hook $HOOK"
  "$HOOK" || log "WARN: $HOOK failed (non-fatal)"
}

# start_new — enable + start NEW; succeeds only when it actually reaches active.
# `enable --now` returning 0 just means systemd attempted the start; a unit that
# crashes immediately (e.g. missing binary) can still exit 0, so we assert
# is-active separately.
start_new() {
  log "starting $NEW ($NEW_UNIT.service)"
  systemctl enable --now "${NEW_UNIT}.service" || true
  systemctl is-active --quiet "${NEW_UNIT}.service"
}

NEW_UNIT="$(unit_for "$NEW")"

# 1. Ensure the target backend is installed AND usable. Its installer owns
#    creating its unit (and declaring a non-default unit name; see unit_for).
#    (openclaw.service is baked by setup.sh and ships no verify hook, so this is a
#    no-op for openclaw — uniform path, no special-case.)
installed_this_run=0
if ! backend_installed; then
  log "$NEW not fully installed (unit missing or verify failed) — installing"
  install_new
  installed_this_run=1
fi
if ! unit_exists "$NEW_UNIT"; then
  log "ERROR: ${NEW_UNIT}.service still absent after install — aborting (not restarting os-server)"
  exit 1
fi

# 2. Optional backend pre-start hook (run before NEW starts).
run_presync

# 3. Start NEW. If it does not reach active and we have NOT already installed it
#    this run, the unit is most likely orphaned (binary missing/broken from a
#    prior half-install or removal) — reinstall once and retry before giving up to
#    the rollback trap. This self-heals an orphaned unit even without a verify
#    hook, so OLD devices recover too.
if ! start_new; then
  if [ "$installed_this_run" = 0 ]; then
    log "WARN: ${NEW_UNIT}.service did not start — unit looks orphaned, reinstalling"
    install_new
    if ! unit_exists "$NEW_UNIT"; then
      log "ERROR: ${NEW_UNIT}.service absent after reinstall — aborting"
      exit 1
    fi
    run_presync
    if ! start_new; then
      log "ERROR: ${NEW_UNIT}.service still not active after reinstall — aborting"
      exit 1
    fi
  else
    log "ERROR: ${NEW_UNIT}.service not active after fresh install — aborting"
    exit 1
  fi
fi

# 3b. NEW is confirmed active — now stop the old backend (os-server tells us which).
if [ -n "$OLD" ] && [ "$OLD" != "$NEW" ]; then
  OLD_UNIT="$(unit_for "$OLD")"
  log "stopping $OLD ($OLD_UNIT.service)"
  stop_unit_retry "$OLD_UNIT"
fi

# 4. Switch is structurally complete: NEW is up and OLD is stopped. Disarm the
# rollback trap. We deliberately do NOT restart os-server here — os-server runs
# this switcher with `systemd-run --wait` and is BLOCKED on our exit code so it
# can report the real outcome (hermes.setup / picoclaw.setup success vs failure)
# before it restarts ITSELF. Restarting os-server from inside this script would
# kill that waiting goroutine mid-ack. So we just exit 0; the caller owns the
# os-server restart (device.Service.RestartForAgentRuntime), which is what makes
# factory.go re-resolve the gateway.
switched=1
log "done — $NEW up, $OLD stopped; os-server restart deferred to caller"
