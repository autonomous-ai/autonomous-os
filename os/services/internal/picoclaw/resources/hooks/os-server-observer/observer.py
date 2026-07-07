#!/usr/bin/env python3
"""os-server-observer — PicoClaw process hook forwarding channel (Telegram) turns
to os-server so they appear in the device Flow Monitor and drive [HW:/…] markers.

Mirror of the Hermes os-server-observer hook, adapted to PicoClaw's process-hook
wire protocol: a long-lived subprocess speaking NDJSON JSON-RPC over stdio — one
JSON object per line on stdin; responses on stdout. PicoClaw spawns it because
config.json has hooks.enabled + hooks.processes.os-server-observer.enabled + this
script's command (see internal/picoclaw/hooks.go).

Wire protocol (verified on-device, picoclaw 0.2.9):
  * hook.hello (REQUEST, has "id") — handshake. Respond {"action":"continue"}.
  * hook.runtime_event (NOTIFICATION, no "id") — params is the events.Event:
    {kind, scope{channel,chat_id,sender_id,session_key,turn_id,…}, payload:{…}}.
      - kind "agent.turn.start": payload.UserMessage is the inbound user text.
      - kind "agent.turn.end":   payload.UserMessage + payload.FinalContent (the
        assistant reply, WITH any [HW:/…] markers). FinalContent is what os-server
        turns into the chat_response + fires HW from.
    Other kinds (agent.tool.*, agent.llm.*) are ignored.
  * Any other REQUEST (before_llm/after_llm if ever configured) — we are in the
    turn's critical path, so respond {"action":"continue"} IMMEDIATELY, never block.

We forward, per turn: agent:start (with UserMessage) on turn.start and agent:end
(with FinalContent) on turn.end, mapped to the ChannelTurn payload os-server's
shared handler already serves for Hermes (handler_channel_turn.go). PicoClaw pairs
these into one Flow turn by session_key.

Only channels in FORWARD_CHANNELS (default: telegram) are forwarded, and internal
senders in SKIP_SENDERS (heartbeat) are dropped — device-local "pico" turns are
already logged by os-server's own sendChat.

Besides POSTing, every forwarded payload is appended as one JSON line (JSONL) to
OBSERVER_LOG (default /root/.picoclaw/logs/messages_hooks.log) — a local audit
trail written before the POST, so it survives an os-server outage.

Set OBSERVER_DEBUG=1 to dump every raw stdin line to stderr (PicoClaw surfaces
subprocess stderr in its gateway log) — used to verify the field names above.
"""

import datetime
import json
import os
import sys
import threading
import urllib.request

OS_SERVER_TURN_URL = "__OS_SERVER_TURN_URL__"
DEBUG = os.environ.get("OBSERVER_DEBUG") == "1"
HOOK_LOG = os.environ.get("OBSERVER_LOG", "/root/.picoclaw/logs/messages_hooks.log")
FORWARD_CHANNELS = {
    c.strip().lower()
    for c in os.environ.get("OBSERVER_CHANNELS", "telegram").split(",")
    if c.strip()
}
# Internal senders whose turns ride a real channel (e.g. the heartbeat turn is
# tagged channel=telegram) but are not user messages — never forward them.
SKIP_SENDERS = {
    s.strip()
    for s in os.environ.get("OBSERVER_SKIP_SENDERS", "heartbeat").split(",")
    if s.strip()
}


def _debug(*a):
    if DEBUG:
        print("[os-server-observer]", *a, file=sys.stderr, flush=True)


def _log_json(record):
    """Append one JSON line to HOOK_LOG. Best-effort + independent of the POST, so
    the local audit trail survives even when os-server is down."""
    try:
        os.makedirs(os.path.dirname(HOOK_LOG), exist_ok=True)
        with open(HOOK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        _debug("log write failed:", e)


def _send(payload):
    try:
        req = urllib.request.Request(
            OS_SERVER_TURN_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).close()
    except Exception as e:  # noqa: BLE001 — best-effort; os-server down must not affect the turn
        _debug("post failed:", e)


def _post(event, ctx):
    payload = {"event": event, "context": ctx}
    # Local audit copy first (synchronous, fast, keeps event order).
    _log_json({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **payload})
    # Fire the network POST OFF the stdin loop so a slow/hung os-server never stalls
    # the next event (observer_timeout_ms is only 500ms). Best-effort daemon thread.
    threading.Thread(target=_send, args=(payload,), daemon=True).start()


def _ctx(scope, message="", response=""):
    """Map a PicoClaw runtime-event scope → the ChannelTurn payload context."""
    return {
        "platform": str(scope.get("channel") or "telegram").lower(),
        "user_id": str(scope.get("sender_id") or ""),
        "chat_id": str(scope.get("chat_id") or ""),
        "session_id": str(scope.get("session_key") or ""),
        "message": message,
        "response": response,
    }


def handle(msg):
    if not isinstance(msg, dict):
        return

    # CRITICAL PATH: answer every request (hook.hello handshake + any intercept)
    # immediately so picoclaw is never blocked waiting on us.
    if msg.get("id") is not None:
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"action": "continue"}})
            + "\n"
        )
        sys.stdout.flush()

    # Only observe runtime events carry the turn boundaries + text we forward.
    if msg.get("method") != "hook.runtime_event":
        return
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return

    kind = str(params.get("kind") or "")
    scope = params.get("scope") or {}
    payload = params.get("payload") or {}
    channel = str(scope.get("channel") or "").lower()
    sender = str(scope.get("sender_id") or "")

    if channel not in FORWARD_CHANNELS:
        return  # device-local "pico" (or other) turns — not ours
    if sender in SKIP_SENDERS or str(scope.get("session_key") or "") in SKIP_SENDERS:
        return  # heartbeat / internal turns riding the channel

    if kind.endswith("turn.start"):
        _post("agent:start", _ctx(scope, message=str(payload.get("UserMessage") or "")))
    elif kind.endswith("turn.end"):
        _post("agent:end", _ctx(scope, response=str(payload.get("FinalContent") or "")))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        _debug("recv:", line[:2000])
        try:
            msg = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _debug("json error:", e)
            continue
        try:
            handle(msg)
        except Exception as e:  # noqa: BLE001 — one bad event must not kill the hook
            _debug("handle error:", e)


if __name__ == "__main__":
    main()
