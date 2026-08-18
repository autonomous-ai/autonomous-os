#!/usr/bin/env bash
# Run a command in a restart loop with structured logging.
# Usage: run-with-restart.sh [OPTIONS] -- COMMAND [ARGS...]
#
# --pid-file PATH:         the inner process PID is written here on each start.
# --wrapper-pid-file PATH: this wrapper's own PID is written here once at startup.
# --cooldown SECONDS:      wait between restarts (default: 5).
# --log-dir PATH:          if set, stdout → log-dir/stdout.log, stderr → log-dir/stderr.log
#                          and watchdog messages → log-dir/watchdog.log.
#                          Plain files on purpose: a pipe to multilog can block the
#                          writer forever if multilog pauses on a write error, which
#                          wedges a single-threaded asyncio server (see MAX_LOG_BYTES).
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

# Largest a log file may reach before the size guard copy-truncates it.
MAX_LOG_BYTES=${MAX_LOG_BYTES:-8388608}   # 8 MiB
LOG_BACKUPS=3
GUARD_INTERVAL=${GUARD_INTERVAL:-60}     # seconds between size checks

# Rename FILE aside on startup, keeping LOG_BACKUPS generations. Safe here because
# no process holds these files open yet.
rotate_on_start() {
    local f=$1 i
    [[ -f "$f" ]] || return 0
    rm -f "$f.$LOG_BACKUPS"
    for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
        [[ -f "$f.$i" ]] && mv -f "$f.$i" "$f.$((i + 1))"
    done
    mv -f "$f" "$f.1"
}

# Cap a live log file. MUST copy-then-truncate, never rename: the child holds an
# O_APPEND fd, so renaming would leave it writing to the renamed inode forever
# (the deleted-but-open pattern that caused the 2026-08-10/17 freezes).
guard_size() {
    local f=$1 i
    [[ -f "$f" ]] || return 0
    local sz
    sz=$(wc -c <"$f" 2>/dev/null || echo 0)
    (( sz < MAX_LOG_BYTES )) && return 0
    rm -f "$f.$LOG_BACKUPS"
    for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
        [[ -f "$f.$i" ]] && mv -f "$f.$i" "$f.$((i + 1))"
    done
    cp -f "$f" "$f.1" && : >"$f"
}

# Set up logging
if [[ -n "$LOG_DIR" ]]; then
    mkdir -p "$LOG_DIR"
    for _f in stdout stderr watchdog; do
        rotate_on_start "$LOG_DIR/$_f.log"
    done
    # Plain append redirect, never a pipe: a failed write returns an error to the
    # writer instead of blocking it forever.
    exec >>"$LOG_DIR/watchdog.log" 2>&1
fi

CHILD_PID=""
GUARD_PID=""
RUNNING=true

# Background size guard: caps the log files while the child runs. Runs in its own
# subshell so the main loop stays blocked on `wait` as before.
start_size_guard() {
    [[ -n "$LOG_DIR" ]] || return 0
    (
        while :; do
            sleep "$GUARD_INTERVAL"
            guard_size "$LOG_DIR/stdout.log"
            guard_size "$LOG_DIR/stderr.log"
            guard_size "$LOG_DIR/watchdog.log"
        done
    ) &
    GUARD_PID=$!
}

stop_size_guard() {
    [[ -n "$GUARD_PID" ]] && kill "$GUARD_PID" 2>/dev/null || true
    GUARD_PID=""
}

cleanup() {
    RUNNING=false
    stop_size_guard
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
}

trap cleanup SIGTERM SIGINT

while $RUNNING; do
    if [[ -n "$LOG_DIR" ]]; then
        "$@" \
            >>"$LOG_DIR/stdout.log" \
            2>>"$LOG_DIR/stderr.log" &
    else
        "$@" &
    fi
    CHILD_PID=$!
    [[ -n "$PID_FILE" ]] && echo "$CHILD_PID" > "$PID_FILE"
    start_size_guard
    EXIT_CODE=0
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""
    stop_size_guard

    if ! $RUNNING; then
        break
    fi

    echo "[watchdog] Process exited (code=$EXIT_CODE), restarting in ${COOLDOWN}s..."
    sleep "$COOLDOWN" &
    wait $! 2>/dev/null || true
done
