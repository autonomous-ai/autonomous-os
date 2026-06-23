#!/usr/bin/env bash
# Run a command in a restart loop with structured logging.
# Usage: run-with-restart.sh [OPTIONS] -- COMMAND [ARGS...]
#
# --pid-file PATH:         the inner process PID is written here on each start.
# --wrapper-pid-file PATH: this wrapper's own PID is written here once at startup.
# --cooldown SECONDS:      wait between restarts (default: 5).
# --log-dir PATH:          if set, stdout → log-dir/stdout/ and stderr → log-dir/stderr/
#                          via multilog. Watchdog messages go to log-dir/watchdog/.
#
# Sending SIGTERM to the wrapper gracefully stops the inner process and exits.

set -euo pipefail

COOLDOWN=5
PID_FILE=""
WRAPPER_PID_FILE=""
LOG_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cooldown) COOLDOWN="$2"; shift 2 ;;
        --pid-file) PID_FILE="$2"; shift 2 ;;
        --wrapper-pid-file) WRAPPER_PID_FILE="$2"; shift 2 ;;
        --log-dir) LOG_DIR="$2"; shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 [--cooldown N] [--pid-file PATH] [--wrapper-pid-file PATH] [--log-dir PATH] -- COMMAND [ARGS...]" >&2
    exit 1
fi

# Write wrapper PID so the stop target can kill us
[[ -n "$WRAPPER_PID_FILE" ]] && echo "$$" > "$WRAPPER_PID_FILE"

# Set up logging
if [[ -n "$LOG_DIR" ]]; then
    mkdir -p "$LOG_DIR/stdout" "$LOG_DIR/stderr" "$LOG_DIR/watchdog"
    # Redirect watchdog's own output to watchdog log
    exec > >(multilog t s1048576 n3 "$LOG_DIR/watchdog") 2>&1
fi

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
    if [[ -n "$LOG_DIR" ]]; then
        "$@" \
            > >(multilog t s1048576 n3 "$LOG_DIR/stdout") \
            2> >(multilog t s1048576 n3 "$LOG_DIR/stderr") &
    else
        "$@" &
    fi
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
