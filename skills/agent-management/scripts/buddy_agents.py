#!/usr/bin/env python3
"""Synchronous device-local agent manager client; never runs a desktop CLI here."""
import argparse
import json
import sys
import urllib.error
import urllib.request

ENDPOINT = "http://127.0.0.1:5000/api/buddy/command"
MAX_BYTES = 3 * 1024 * 1024


class BuddyError(Exception):
    """Rejected command or uncertain delivery; callers must not replay automatically."""


class BuddyRejected(BuddyError):
    """The desktop returned an explicit rejection, rather than losing the response."""
    rejected = True


def command(action, params, endpoint=ENDPOINT):
    if action not in {"list", "create", "send", "session", "stop"}:
        raise BuddyError("unknown agent action")
    if not isinstance(params, dict):
        raise BuddyError("params must be a JSON object")
    required = {"create": ["project_id", "request_id", "provider"],
                "send": ["project_id", "session_id", "request_id", "prompt"],
                "session": ["project_id", "session_id"],
                "stop": ["project_id", "session_id"]}.get(action, [])
    for key in required:
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise BuddyError(f"{key} must be a nonempty string")
    if "after_seq" in params and (type(params["after_seq"]) is not int or params["after_seq"] < 0):
        raise BuddyError("after_seq must be a nonnegative integer")
    try:
        body = json.dumps({"action": "agent." + action, "params": params,
                           "timeout_ms": 10000}, allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise BuddyError("params must be finite JSON values") from exc
    if len(body) > 1024 * 1024:
        raise BuddyError("command exceeds 1 MiB")
    request = urllib.request.Request(endpoint, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BuddyError("Buddy request failed or timed out; delivery uncertain. Inspect session; do not resend automatically.") from exc
    if len(raw) > MAX_BYTES:
        raise BuddyError("Buddy response exceeds 3 MiB")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BuddyError("Buddy returned invalid JSON") from exc
    if not isinstance(envelope, dict) or type(envelope.get("status")) is not int or envelope["status"] != 1:
        raise BuddyError("Buddy API rejected the request")
    result = envelope.get("data")
    if isinstance(result, dict) and result.get("ok") is False:
        raise BuddyRejected(str(result.get("error", "Buddy action failed")))
    if not isinstance(result, dict) or result.get("ok") is not True or "result" not in result:
        raise BuddyError("Malformed Buddy response; delivery uncertain")
    return result.get("result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "create", "send", "session", "stop", "voice"])
    parser.add_argument("params", nargs="?", default="{}", help="JSON object, or - to read stdin")
    args = parser.parse_args()
    try:
        params = json.loads(sys.stdin.read() if args.params == "-" else args.params)
        if args.action == "voice":
            from voice_router import VoiceRouter, RoutingError
            try:
                result = VoiceRouter(command).run(params)
            except RoutingError as exc:
                raise BuddyError(str(exc)) from exc
        else:
            result = command(args.action, params)
        print(json.dumps(result, ensure_ascii=False))
    except (BuddyError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
