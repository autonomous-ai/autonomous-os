#!/usr/bin/env bash
# Deploy HAL and/or os-server to ONE device on the LAN, by IP.
#
# WHY THIS SCRIPT: `make upload-hal` publishes an OTA release to the whole
# fleet (bumps VERSION_HAL, signs, uploads). That is the wrong tool for
# "push my working tree to the lamp on my desk". This does the dev push:
# rsync the tree, swap the binary, restart the units.
#
# USAGE:
#   scripts/deploy-device.sh --host 172.168.20.255              # hal + os-server
#   scripts/deploy-device.sh --host 172.168.20.255 --hal        # hal only
#   scripts/deploy-device.sh --host 172.168.20.255 --os-server  # binary only
#   scripts/deploy-device.sh --host lamp.local --user pi --pass secret
#   scripts/deploy-device.sh --host 172.168.20.255 --hal --dry-run
#
# --dry-run lists what WOULD change on the device and exits. Worth running
# whenever your branch might be behind what is deployed: the swap has no
# --delete, but it does overwrite, so a stale checkout can silently revert
# work that only exists on the device.
#
# Or via make:  IP=172.168.20.255 make device-deploy
#
# AUTH: password auth when PI_PASS is set (default "orangepi", needs sshpass);
# set PI_PASS="" to use your SSH key + interactive sudo instead.
#
# NEVER TOUCHED on the device: .env (device-local tuning), .venv, and
# calibration/ (hand-recaptured servo poses). No --delete, so device-local
# paths outside the repo survive.
set -euo pipefail

HOST="${PI_HOST:-${IP:-}}"
USER="${PI_USER:-orangepi}"
PASS="${PI_PASS-orangepi}"
DO_HAL=0
DO_OS=0
SKIP_BUILD=0
DRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)      HOST="$2"; shift 2 ;;
    --user)      USER="$2"; shift 2 ;;
    --pass)      PASS="$2"; shift 2 ;;
    --hal)       DO_HAL=1; shift ;;
    --os-server) DO_OS=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help)   sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# No component flag = both.
if [[ $DO_HAL -eq 0 && $DO_OS -eq 0 ]]; then DO_HAL=1; DO_OS=1; fi

if [[ -z "$HOST" ]]; then
  echo "ERROR: no target. Pass --host <ip> or set IP=<ip>." >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$REPO_ROOT/system/os-server"
STAGE="/home/$USER/.deploy-stage"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR -o ConnectTimeout=10)

# Password auth is optional: with PI_PASS="" fall back to key auth, so this
# works unattended in a shell that already has an agent.
if [[ -n "$PASS" ]]; then
  SSHPASS_BIN="$(command -v sshpass || echo "$HOME/.local/bin/sshpass")"
  if [[ ! -x "$SSHPASS_BIN" ]]; then
    echo "ERROR: sshpass not found (needed for password auth)." >&2
    echo "  brew install hudochenkov/sshpass/sshpass   — or set PI_PASS=\"\" to use your SSH key." >&2
    exit 1
  fi
  SSH=(env "SSHPASS=$PASS" "$SSHPASS_BIN" -e ssh "${SSH_OPTS[@]}")
  SCP=(env "SSHPASS=$PASS" "$SSHPASS_BIN" -e scp "${SSH_OPTS[@]}")
  RSYNC_RSH="$SSHPASS_BIN -e ssh ${SSH_OPTS[*]}"
  RSYNC_ENV=(env "SSHPASS=$PASS")
  # `sudo -S` reads the password from stdin; -p "" keeps the prompt out of the log.
  SUDO="echo '$PASS' | sudo -S -p ''"
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp "${SSH_OPTS[@]}")
  RSYNC_RSH="ssh ${SSH_OPTS[*]}"
  RSYNC_ENV=(env)
  SUDO="sudo"
fi

echo "=== Preflight: $USER@$HOST ==="
ping -c 1 -W 2 "$HOST" >/dev/null 2>&1 || {
  echo "ERROR: $HOST unreachable." >&2; exit 1; }
"${SSH[@]}" "$USER@$HOST" true || {
  echo "ERROR: SSH to $USER@$HOST failed (wrong user/password/key?)." >&2; exit 1; }
echo "OK — $("${SSH[@]}" "$USER@$HOST" 'hostname; uname -m' | paste -sd' ' -)"
# rsync creates the staging dir on its own; scp does not, so make it here or
# an --os-server-only run has nowhere to land.
"${SSH[@]}" "$USER@$HOST" "mkdir -p '$STAGE'"

if [[ $DO_OS -eq 1 && $SKIP_BUILD -eq 0 ]]; then
  echo
  echo "=== Build os-server (linux/arm64) ==="
  make -C "$REPO_ROOT" os-build
fi
if [[ $DO_OS -eq 1 && ! -f "$BIN" ]]; then
  echo "ERROR: $BIN missing — run 'make os-build'." >&2; exit 1
fi

HAL_EXCLUDES=(--exclude __pycache__ --exclude '*.pyc' --exclude .pytest_cache
  --exclude .venv --exclude '.venv-*' --exclude .env
  --exclude calibration --exclude recordings)

if [[ $DRY -eq 1 ]]; then
  echo
  if [[ $DO_HAL -eq 1 ]]; then
    echo "=== DRY RUN: hal/ files that would change on $HOST ==="
    # No sudo: a dry run only reads, and /opt/hal is world-readable. Routing it
    # through `sudo -S` would need a pipe in --rsync-path, and that echo lands
    # in the rsync protocol stream and kills the connection.
    if ! out="$("${RSYNC_ENV[@]}" rsync -rn --checksum --itemize-changes "${HAL_EXCLUDES[@]}" \
        -e "$RSYNC_RSH" "$REPO_ROOT/hal/" "$USER@$HOST:/opt/hal/" 2>&1)"; then
      printf '%s\n' "$out" >&2
      echo "ERROR: dry run failed — refusing to report 'in sync'." >&2
      exit 1
    fi
    changed="$(printf '%s\n' "$out" | grep '^[<>ch]' || true)"
    if [[ -n "$changed" ]]; then
      printf '%s\n' "$changed"
      printf '  → %s file(s) would be overwritten on the device\n' \
        "$(printf '%s\n' "$changed" | wc -l | tr -d ' ')"
    else
      echo "  (in sync)"
    fi
  fi
  if [[ $DO_OS -eq 1 ]]; then
    echo "=== DRY RUN: would install $BIN → /usr/local/bin/os-server ==="
  fi
  echo "(nothing was changed)"
  exit 0
fi

if [[ $DO_HAL -eq 1 ]]; then
  echo
  echo "=== 1) rsync hal/ → $STAGE ==="
  # Staged first: /opt/hal is root-owned and $USER's sudo may need a password,
  # so rsync cannot write there directly as $USER.
  "${RSYNC_ENV[@]}" rsync -a --info=stats1 "${HAL_EXCLUDES[@]}" \
    -e "$RSYNC_RSH" "$REPO_ROOT/hal/" "$USER@$HOST:$STAGE/hal/"

  echo
  echo "=== 2) swap into /opt/hal + restart hal ==="
  "${SSH[@]}" "$USER@$HOST" "$SUDO sh -c '
    rsync -a --exclude __pycache__ --exclude .venv --exclude .env --exclude calibration \
      $STAGE/hal/ /opt/hal/ &&
    find /opt/hal -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null;
    systemctl restart hal'"

  echo "   waiting for hal ..."
  "${SSH[@]}" "$USER@$HOST" '
    for i in $(seq 1 60); do
      if [ "$(curl -s -o /dev/null -w %{http_code} --max-time 3 http://127.0.0.1:5001/health)" = "200" ]; then
        echo "   hal healthy after ${i}s"; exit 0
      fi
      sleep 1
    done
    echo "   WARNING: hal did not answer /health within 60s"; exit 1'
fi

if [[ $DO_OS -eq 1 ]]; then
  echo
  echo "=== 3) scp os-server + restart ==="
  "${SCP[@]}" "$BIN" "$USER@$HOST:$STAGE/os-server.new"
  "${SSH[@]}" "$USER@$HOST" "$SUDO sh -c '
    install -m 0755 -o root -g root $STAGE/os-server.new /usr/local/bin/os-server &&
    systemctl restart os-server'"
fi

echo
echo "=== Result ==="
"${SSH[@]}" "$USER@$HOST" '
  printf "  hal        %s (v%s)\n" "$(systemctl is-active hal)" "$(cat /opt/hal/VERSION_HAL 2>/dev/null || echo ?)"
  printf "  os-server  %s\n" "$(systemctl is-active os-server)"'
echo "✔ deploy complete."
