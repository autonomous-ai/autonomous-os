#!/usr/bin/env bash
# rev: 2026-09-09T08:45:07Z
# hermes-lan-setup.sh — configure Hermes on this Mac to accept remote agent
# connections from an Intern 2 device on the same LAN.
#
# What it does, in this order:
#   1. Finds the Hermes install (grid-managed at ~/.grid/tools/hermes-agent,
#      or an explicit path via HERMES_ROOT).
#   2. Ensures aiohttp is present in Hermes's Python env — the API server
#      adapter refuses to bind without it.
#   3. Idempotently writes API_SERVER_* into ~/.hermes/.env, generating a
#      fresh random token when one is not already stored.
#   4. Reloads Hermes:
#        - Restarts the running foreground gateway if there is one, or
#        - Installs a launchd background service on Apple Silicon macs so the
#          gateway survives logout/reboot (system-level; needs sudo).
#   5. Prints the two fields the operator pastes into the device:
#        Hermes URL: http://<lan-ip>:8642
#        API Key:    <token>
#
# Usage on the Mac (single line):
#   curl -fsSL https://cdn.autonomous.ai/os/tools/hermes-lan-setup.sh | bash
#
# Overrides (env vars):
#   HERMES_ROOT     override auto-detected Hermes install directory
#   HERMES_PORT     override the API server port (default 8642 — matches the
#                   device's runtimes/hermes/constants.go BaseURL)
#   HERMES_HOST     override the bind address (default 0.0.0.0 — LAN-reachable)
#   HERMES_TOKEN    force a specific token instead of a generated one

set -uo pipefail

log()  { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. locate the Hermes CLI + its Python interpreter ───────────────────────
# The CLI's location is the ground truth: whatever python actually runs Hermes
# is what we install aiohttp into. Everything else (grid/brew/pip/pipx/uv paths)
# is a fallback that just helps discovery when `hermes` isn't on PATH.
HERMES_BIN="${HERMES_BIN:-}"
if [ -z "$HERMES_BIN" ] && [ -n "${HERMES_ROOT:-}" ] && [ -x "$HERMES_ROOT/bin/hermes" ]; then
  HERMES_BIN="$HERMES_ROOT/bin/hermes"
fi
if [ -z "$HERMES_BIN" ]; then
  HERMES_BIN="$(command -v hermes 2>/dev/null || true)"
fi
if [ -z "$HERMES_BIN" ]; then
  # Search common install locations. Each entry is one CLI path — the point of
  # walking these is to find the CLI, not to gate on a specific layout.
  for candidate in \
    "$HOME/.grid/tools/hermes-agent/bin/hermes" \
    "$HOME/.local/bin/hermes" \
    "$HOME/.local/pipx/venvs/hermes-agent/bin/hermes" \
    "$HOME/.local/share/pipx/venvs/hermes-agent/bin/hermes" \
    "$HOME/Library/Python/3.13/bin/hermes" \
    "$HOME/Library/Python/3.12/bin/hermes" \
    "$HOME/Library/Python/3.11/bin/hermes" \
    "/opt/homebrew/bin/hermes" \
    "/usr/local/bin/hermes"; do
    if [ -x "$candidate" ]; then
      HERMES_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$HERMES_BIN" ]; then
  die "hermes CLI not found. If Hermes is installed but not on PATH, set HERMES_BIN=/path/to/hermes and re-run. If Hermes Desktop is installed, enable the API server in its Settings tab instead — this script drives the CLI install."
fi

# Resolve the Python interpreter that actually runs Hermes. Every install
# style stashes this differently:
#   - grid / brew / pipx classic: shebang line is the venv's python
#   - #!/usr/bin/env pythonX.Y: shebang is env, second word is the python name
#   - pipx bash wrappers: shebang is /bin/bash, the real python path lives
#     inside the script as an `exec …/python …` line
#   - Nothing above: fall back to sibling python next to the CLI, then to
#     pipx venvs in the user's home, then to whichever python3 knows `hermes`.
# HERMES_PY env var short-circuits every heuristic.
HERMES_PY="${HERMES_PY:-}"

if [ -z "$HERMES_PY" ]; then
  shebang="$(head -1 "$HERMES_BIN" 2>/dev/null)"
  candidate="$(printf '%s' "$shebang" | sed -n 's|^#![[:space:]]*\(/[^[:space:]]*\).*|\1|p')"
  case "$candidate" in
    */env)
      # `#!/usr/bin/env pythonX.Y` — the interpreter name is the second word.
      env_arg="$(printf '%s' "$shebang" | awk '{print $2}')"
      resolved="$(command -v "$env_arg" 2>/dev/null || true)"
      [ -n "$resolved" ] && HERMES_PY="$resolved"
      ;;
    */bash|*/sh|*/zsh|*/dash|*/ksh)
      # Shell wrapper. Two shapes we handle:
      #   1) exec "/abs/path/to/python" ...      — grep an absolute python path
      #   2) exec "$DIR/../venv/bin/python" ...  — the python is one shell var
      #      away, so we run the wrapper under `bash -x` and read the trace,
      #      which prints the final expanded exec argv on stderr.
      # Shape (1) first (cheap). Shape (2) as a follow-up when (1) misses.
      resolved="$(grep -oE '/[[:alnum:]/._-]+/python(3(\.[0-9]+)?)?' "$HERMES_BIN" 2>/dev/null | head -1)"
      if [ -z "$resolved" ] || [ ! -x "$resolved" ]; then
        # Trace the wrapper to see the python it exec's. --help is a light,
        # side-effect-free subcommand; 2>&1 captures the +exec line the shell
        # prints to stderr when `set -x` is on. Any failure here is fine —
        # the outer fallbacks below still run.
        trace_py="$(bash -x "$HERMES_BIN" --help </dev/null 2>&1 | \
          grep -oE '/[[:alnum:]/._-]+/python(3(\.[0-9]+)?)?' | head -1)"
        [ -n "$trace_py" ] && [ -x "$trace_py" ] && resolved="$trace_py"
      fi
      [ -n "$resolved" ] && [ -x "$resolved" ] && HERMES_PY="$resolved"
      ;;
    /*)
      # Direct python shebang.
      [ -x "$candidate" ] && HERMES_PY="$candidate"
      ;;
  esac
fi

# Sibling python next to the CLI covers most venv layouts we haven't caught above.
if [ -z "$HERMES_PY" ]; then
  bin_dir="$(dirname "$HERMES_BIN")"
  for name in python3 python; do
    if [ -x "$bin_dir/$name" ]; then
      HERMES_PY="$bin_dir/$name"
      break
    fi
  done
fi

# Known venv layouts by install method. Each hermes install method drops its
# venv in a different place; walking a fixed list is more predictable than
# guessing dynamically. Add new layouts here as they turn up.
if [ -z "$HERMES_PY" ]; then
  for candidate in \
    "$HOME/.hermes/hermes-agent/venv/bin/python" \
    "$HOME/.hermes/venv/bin/python" \
    "$HOME/hermes-agent/venv/bin/python" \
    "$HOME/.local/pipx/venvs/hermes-agent/bin/python" \
    "$HOME/.local/share/pipx/venvs/hermes-agent/bin/python" \
    "/opt/homebrew/opt/hermes-agent/venv/bin/python" \
    "/usr/local/opt/hermes-agent/venv/bin/python"; do
    if [ -x "$candidate" ]; then
      HERMES_PY="$candidate"
      break
    fi
  done
fi

# Last resort: any python3 on PATH that can import hermes. This catches
# pip/pipx installs whose wrapper we couldn't parse but whose site-packages
# is on the system python's search path.
if [ -z "$HERMES_PY" ]; then
  for candidate in $(command -v python3 python 2>/dev/null); do
    if "$candidate" -c "import hermes" 2>/dev/null; then
      HERMES_PY="$candidate"
      break
    fi
  done
fi

# Sanity: whatever we ended up with MUST be a Python. A misdetect (e.g. a
# shell wrapper whose absolute python path grep matched the wrong line) would
# silently pass down as HERMES_PY and blow up at the aiohttp step with a
# confusing "pip failed" error. Force `import sys` — if that fails, clear
# HERMES_PY and let the operator see the real error message below.
if [ -n "$HERMES_PY" ] && ! "$HERMES_PY" -c 'import sys' 2>/dev/null; then
  HERMES_PY=""
fi

if [ -z "$HERMES_PY" ] || [ ! -x "$HERMES_PY" ]; then
  cat >&2 <<EOF
✗ Could not resolve Python interpreter for $HERMES_BIN.

Find the python that runs your Hermes install:
  find \$HOME/.hermes \$HOME/hermes-agent \$HOME/.local -name python -path '*/venv/*' 2>/dev/null
or:
  head -50 $HERMES_BIN | grep -o '/[^ ]*python[0-9.]*' | head -1

Then re-run with an override on the RIGHT side of the pipe (env must apply to bash, not curl):
  curl -fsSL "https://cdn.autonomous.ai/os/tools/hermes-lan-setup.sh?v=\$(uuidgen)" | HERMES_PY=/full/path/to/python bash
EOF
  exit 1
fi

log "Hermes CLI     : $HERMES_BIN"
log "Hermes Python  : $HERMES_PY"

# ── 2. ensure aiohttp is available to the Hermes CLI's Python ───────────────
# `hermes gateway run` refuses to bind the API server without it. Try the CLI's
# own python first; it's a no-op when aiohttp is already there. When aiohttp is
# missing, `uv pip` covers uv-style venvs (grid + some pipx installs) that have
# no `pip` module inside; `python -m pip` covers the classical layouts.
if ! "$HERMES_PY" -c "import aiohttp" 2>/dev/null; then
  log "Installing aiohttp for Hermes (one-off)…"
  installed=0
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$HERMES_PY" aiohttp >/dev/null 2>&1 && installed=1
  fi
  if [ "$installed" -eq 0 ]; then
    if "$HERMES_PY" -m pip --version >/dev/null 2>&1 \
       || "$HERMES_PY" -m ensurepip >/dev/null 2>&1; then
      "$HERMES_PY" -m pip install --user aiohttp >/dev/null 2>&1 && installed=1
    fi
  fi
  [ "$installed" -eq 1 ] || die "Failed to install aiohttp. Install uv (https://docs.astral.sh/uv/) or ensure pip works for $HERMES_PY, then re-run."
fi
ok "aiohttp available to Hermes"

# ── 3. write API_SERVER_* into ~/.hermes/.env ────────────────────────────────
HERMES_DIR="$HOME/.hermes"
ENV_FILE="$HERMES_DIR/.env"
mkdir -p "$HERMES_DIR"
touch "$ENV_FILE"

HERMES_PORT="${HERMES_PORT:-8642}"
HERMES_HOST="${HERMES_HOST:-0.0.0.0}"

# Prefer a token the user explicitly passed in; otherwise reuse the one already
# in the env file so re-running the script doesn't rotate an in-use key;
# otherwise generate a fresh one. Format is deliberately readable so people can
# type it if they need to.
existing_token="$(awk -F= '/^API_SERVER_KEY=/{print $2; exit}' "$ENV_FILE" 2>/dev/null || true)"
if [ -n "${HERMES_TOKEN:-}" ]; then
  TOKEN="$HERMES_TOKEN"
elif [ -n "$existing_token" ]; then
  TOKEN="$existing_token"
else
  TOKEN="intern2-hermes-$(openssl rand -hex 8)"
fi

# Strip old lines then append fresh ones — idempotent.
for k in API_SERVER_ENABLED API_SERVER_HOST API_SERVER_PORT API_SERVER_KEY; do
  # Portable in-place edit (Mac sed and GNU sed both accept -i.bak).
  sed -i.bak "/^${k}=/d" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
done
# Guarantee a trailing newline before appending so we don't jam onto a line.
[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
{
  echo ""
  echo "# --- Intern 2 remote-agent bridge (added $(date -u +%FT%TZ)) ---"
  echo "API_SERVER_ENABLED=true"
  echo "API_SERVER_HOST=$HERMES_HOST"
  echo "API_SERVER_PORT=$HERMES_PORT"
  echo "API_SERVER_KEY=$TOKEN"
} >> "$ENV_FILE"
ok "~/.hermes/.env updated (API_SERVER_ENABLED + host + port + key)"

# ── 4. reload Hermes so the new config takes effect ──────────────────────────
# Detect a running foreground gateway. If found → restart it. If not → try to
# install as a launchd service (persistent). Both paths tolerate failure and
# fall back to a plain foreground start so the operator always ends up with a
# listening server.
running_pid="$(pgrep -f 'hermes gateway run' | head -1 || true)"
if [ -n "$running_pid" ]; then
  log "Foreground gateway running (pid=$running_pid) — restarting so it picks up the new env…"
  # SIGTERM first, then SIGKILL if the process ignores it. Wait for the port
  # to actually clear before restarting: macOS holds a socket in TIME_WAIT
  # after a hard kill and the second gateway would fail to bind. Polling
  # lsof is dependable in a way that a fixed sleep is not.
  kill -TERM "$running_pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    kill -0 "$running_pid" 2>/dev/null || break
  done
  kill -0 "$running_pid" 2>/dev/null && kill -KILL "$running_pid" 2>/dev/null || true
  # Wait up to 15s for the port to be released. On macOS a TIME_WAIT socket
  # blocks a fresh bind even after the owning process is gone; SO_REUSEADDR
  # would fix it in-process but we don't control that source.
  for _ in $(seq 1 15); do
    if ! lsof -iTCP:"$HERMES_PORT" -sTCP:LISTEN -n >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  nohup "$HERMES_BIN" gateway run >/tmp/hermes-gateway.log 2>&1 &
  ok "Foreground gateway restarted (log: /tmp/hermes-gateway.log)"
else
  # Try the launchd service path so the gateway survives a reboot/logout.
  # `hermes gateway install` writes a per-user LaunchAgent; this is the
  # recommended production shape on macOS.
  log "Installing launchd service (persistent across reboots)…"
  if yes y | "$HERMES_BIN" gateway install >/tmp/hermes-install.log 2>&1; then
    ok "launchd service installed"
  else
    warn "launchd install returned non-zero — falling back to a foreground gateway"
    nohup "$HERMES_BIN" gateway run >/tmp/hermes-gateway.log 2>&1 &
    ok "Foreground gateway started (log: /tmp/hermes-gateway.log)"
  fi
fi

# Give the socket a moment to bind, then verify.
sleep 4
if lsof -iTCP:"$HERMES_PORT" -sTCP:LISTEN -n 2>/dev/null | grep -q "$HERMES_PORT"; then
  ok "Hermes API server listening on $HERMES_HOST:$HERMES_PORT"
else
  warn "Hermes API server didn't come up on $HERMES_PORT — check /tmp/hermes-gateway.log or /tmp/hermes-install.log"
fi

# ── 5. print the two fields the device needs ─────────────────────────────────
# Prefer the Wi-Fi interface (en0), then wired (en1). ipconfig is macOS-only —
# the script is macOS-only, so that's fine.
LAN_IP=""
for iface in en0 en1 en2 en3; do
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  if [ -n "$ip" ]; then
    LAN_IP="$ip"
    break
  fi
done
[ -n "$LAN_IP" ] || LAN_IP="<your-mac-lan-ip>"

echo
printf '\033[1m'
echo "════════════════════════════════════════════════════════════════"
echo "  Paste these into your Intern 2 web setup page"
echo "  (Settings → Runtime → Remote (external))"
echo "════════════════════════════════════════════════════════════════"
printf '\033[0m'
echo
echo "  Hermes URL : http://$LAN_IP:$HERMES_PORT"
echo "  API Key    : $TOKEN"
echo
echo "Verify from another machine on the LAN:"
echo "  curl -H 'Authorization: Bearer $TOKEN' http://$LAN_IP:$HERMES_PORT/health"
echo
