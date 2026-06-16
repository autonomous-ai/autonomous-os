#!/usr/bin/env bash
# Run a command in a restart loop. Restarts on exit with a cooldown.
# Usage: run-with-restart.sh [--cooldown SECONDS] [--pid-file PATH] [--wrapper-pid-file PATH] -- COMMAND [ARGS...]
#
# --pid-file:         the inner process PID is written here on each start.
# --wrapper-pid-file: this wrapper's own PID is written here once at startup.
# Sending SIGTERM to the wrapper gracefully stops the inner process and exits.

set -euo pipefail

COOLDOWN=5
PID_FILE=""
WRAPPER_PID_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cooldown) COOLDOWN="$2"; shift 2 ;;
        --pid-file) PID_FILE="$2"; shift 2 ;;
        --wrapper-pid-file) WRAPPER_PID_FILE="$2"; shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 [--cooldown N] [--pid-file PATH] [--wrapper-pid-file PATH] -- COMMAND [ARGS...]" >&2
    exit 1
fi

# Write wrapper PID so the stop target can kill us
[[ -n "$WRAPPER_PID_FILE" ]] && echo "$$" > "$WRAPPER_PID_FILE"

CHILD_PID=""
RUNNING=true

cleanup() {
    RUNNING=false
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
}

trap cleanup SIGTERM SIGINT

while $RUNNING; do
    "$@" &
    CHILD_PID=$!
    [[ -n "$PID_FILE" ]] && echo "$CHILD_PID" > "$PID_FILE"
    EXIT_CODE=0
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""

    if ! $RUNNING; then
        break
    fi

    echo "[watchdog] Process exited (code=$EXIT_CODE), restarting in ${COOLDOWN}s..."
    sleep "$COOLDOWN" &
    wait $! 2>/dev/null || true
done
