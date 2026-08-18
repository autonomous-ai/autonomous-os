#!/usr/bin/env bash
# Stop a perception-service process tree and PROVE it stopped.
#
# Usage: stop-tree.sh NAME PORT WRAPPER_PID_FILE PID_FILE
#
# The old `make stop-runpod-*` bodies sent one SIGTERM to two PIDs read from
# /tmp, slept 2 seconds, and printed "stopped" unconditionally. Three problems,
# all of which cost real outage time:
#
#   1. SIGTERM cannot stop a hung server. uvicorn's handler only sets
#      should_exit, and the only thing that can act on it is the event loop --
#      the thing that is stuck. Escalation to -9 is required.
#   2. The pid files are least trustworthy exactly when stop matters most: a
#      failed start overwrites them with its own dead PIDs (2026-08-10).
#      The listening socket is authoritative, so ask the kernel instead.
#   3. Reporting success without checking meant the next `start` died on
#      EADDRINUSE with no clue why. This exits non-zero instead.
set -uo pipefail

NAME=${1:?usage: stop-tree.sh NAME PORT WRAPPER_PID_FILE PID_FILE}
PORT=${2:?}
WPID_FILE=${3:?}
PID_FILE=${4:?}

# Every PID belonging to this service, from every source we have. The process
# group covers anything the wrapper spawned (child, size guard, liveness probe);
# the port and pid files catch instances started by an older build that predates
# setsid.
collect() {
    {
        ss -lntpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2
        [[ -r "$WPID_FILE" ]] && cat "$WPID_FILE"
        [[ -r "$PID_FILE"  ]] && cat "$PID_FILE"
        pgrep -f "run-with-restart.sh .*--log-dir /workspace/logs/$NAME" 2>/dev/null
        pgrep -f "python -m $NAME " 2>/dev/null
    } 2>/dev/null | grep -E '^[0-9]+$' | sort -un
}

# Expand each PID to its whole process group so nothing is orphaned.
#
# SAFETY: never return our own process group. Start targets use `setsid`, so a
# current-build wrapper always has a group of its own. But an instance started by
# an older build shares the group of whatever launched it -- make, or the
# operator's shell. Killing that group would take down make (and this script)
# mid-stop, which looks exactly like a successful stop while leaving the server
# running. Such PIDs are still killed individually below.
expand_groups() {
    local pid pgid self
    self=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
    for pid in "$@"; do
        pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
        [[ -z "$pgid" ]] && continue
        if [[ "$pgid" == "$self" ]]; then
            echo "[$NAME] pid $pid shares our process group ($pgid); killing it" \
                 "individually instead of by group" >&2
            continue
        fi
        (( pgid > 1 )) && echo "$pgid"
    done | sort -un
}

# Test collect's OUTPUT, never its exit status. `set -o pipefail` is on and the
# last producer inside collect is pgrep, which exits 1 when it matches nothing --
# so collect can exit non-zero while still having printed live PIDs. Keying on the
# status made `alive` report "dead" for a live process, which broke out of the
# escalation loop after the first SIGTERM and meant KILL was never sent.
alive() { [[ -n "$(collect | tr -d '[:space:]')" ]]; }

# No mapfile/readarray here: this script has to work in a crisis, so it must not
# assume bash >= 4.
for sig in TERM TERM KILL; do
    pids=$(collect | tr '\n' ' ')
    [[ -z "${pids// /}" ]] && break
    # shellcheck disable=SC2086
    pgids=$(expand_groups $pids | tr '\n' ' ')

    echo "[$NAME] SIG$sig -> pids: ${pids:-none} groups: ${pgids:-none}"
    for pgid in $pgids; do kill -"$sig" -- "-$pgid" 2>/dev/null || true; done
    for pid  in $pids;  do kill -"$sig" "$pid"       2>/dev/null || true; done

    for _ in $(seq 20); do          # up to 10s per round
        sleep 0.5
        alive || break 2
    done
done

rm -f "$WPID_FILE" "$PID_FILE"

# Verify. Never claim success we have not proven.
leftover=$(collect | tr '\n' ' ')
if [[ -n "${leftover// /}" ]] || ss -lntH "sport = :$PORT" 2>/dev/null | grep -q .; then
    echo "[$NAME] FAILED to stop cleanly." >&2
    echo "[$NAME]   survivors: ${leftover:-none}" >&2
    ss -lntp "sport = :$PORT" >&2 2>/dev/null || true
    exit 1
fi

echo "[$NAME] stopped, port $PORT released"
