"""os-server-observer — forwards every Hermes gateway turn to os-server.

Why: the Hermes gateway owns all messaging-channel I/O (Telegram/Slack/Discord/…)
and replies directly, so os-server never sees those turns and they don't show up
in the device Flow Monitor (unlike OpenClaw, which pushes session.message events).
The gateway has no cross-platform turn broadcast, but it DOES fire hooks on the
shared agent pipeline that every platform flows through. This hook posts each turn
to os-server, which emits the flow events that light up Flow Monitor.

Contract (gateway/hooks.py): a top-level `handle(event_type, context)`, sync or
async, registered for the events listed in HOOK.yaml. Hook errors are caught by
the gateway and never block the turn, but we also swallow our own errors so a
flaky os-server can't slow the agent down.

This file is MATERIALIZED by os-server (internal/hermes/hooks.go) into
~/.hermes/hooks/os-server-observer/ on every boot. The __OS_SERVER_TURN_URL__
placeholder is substituted with the real loopback URL at materialize time.
Channel-agnostic: os-server filters by the `platform` field, so adding a channel
needs no change here.
"""

import asyncio
import json
import urllib.request

OS_SERVER_TURN_URL = "__OS_SERVER_TURN_URL__"


def _post(body: bytes) -> None:
    req = urllib.request.Request(
        OS_SERVER_TURN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:
        # Best-effort: os-server unreachable / restarting must not affect the turn.
        pass


async def handle(event_type, context):
    try:
        body = json.dumps({"event": event_type, "context": context}).encode("utf-8")
    except (TypeError, ValueError):
        return
    # Run the blocking POST off the event loop so the agent pipeline never stalls.
    await asyncio.to_thread(_post, body)
