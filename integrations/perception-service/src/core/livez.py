"""Liveness endpoint shared by dlserver and lbserver.

Liveness answers exactly one question: *is this process still executing?* It must
therefore do as little as possible -- no auth, no model checks, no downstream
calls, no locks, no I/O. If this handler cannot produce a response, the asyncio
event loop is not running, which is the failure this endpoint exists to catch
(dlserver 2026-08-17: alive, port bound, LISTEN, event loop blocked in
pipe_write, every port-based check green for 50 minutes).

This is deliberately NOT the same as readiness. dlserver's /hal/api/dl/health
reports whether the models finished loading; a watchdog that restarted on *that*
would kill the process during its ~2 minute model load and never finish booting.
Readiness failing means "route traffic elsewhere"; liveness failing means
"restart this process".
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/livez")
async def livez() -> dict[str, str]:
    """Return 200 iff a coroutine can still be scheduled on the event loop."""
    return {"status": "alive"}
