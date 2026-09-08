#!/usr/bin/env python3
"""Device-side synchronous Buddy client; Python standard library only."""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import uuid


ENDPOINT = "http://127.0.0.1:5000/api/buddy/command"
OBSERVE_ENDPOINT = "http://127.0.0.1:5000/api/buddy/observe"
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_CAPTURES = 50


class BuddyError(Exception):
    """An invalid command, transport failure, or rejected Buddy action."""


def observe(question, params, endpoint=OBSERVE_ENDPOINT):
    """Ask the device's configured auxiliary vision model about a fresh Mac capture."""
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise BuddyError("question must contain 1–2000 characters")
    if not isinstance(params, dict) or set(params) - {"display_id", "scale"}:
        raise BuddyError("observe params may contain only display_id and scale")
    if "display_id" in params and (type(params["display_id"]) is not int or not 1 <= params["display_id"] <= 4294967295):
        raise BuddyError("display_id must be a positive unsigned 32-bit integer")
    scale = params.get("scale", 0.5)
    if type(scale) not in (int, float) or not 0.01 <= scale <= 1:
        raise BuddyError("scale must be between 0.01 and 1")
    payload = dict(params, question=question, scale=scale)
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=90) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            failure = json.loads(exc.read(8192))
            detail = failure.get("message") if isinstance(failure, dict) else None
        except (ValueError, OSError):
            detail = None
        raise BuddyError(f"desktop observation HTTP {exc.code}: {detail or 'request failed'}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BuddyError("desktop observation unavailable or timed out; no visual result was obtained") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BuddyError("desktop observation response exceeds 24 MiB")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BuddyError("desktop observation returned invalid JSON") from exc
    if not isinstance(envelope, dict) or type(envelope.get("status")) is not int or envelope["status"] != 1:
        detail = envelope.get("message") if isinstance(envelope, dict) else None
        raise BuddyError(str(detail or "desktop observation failed"))
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("description"), str) or not data["description"].strip():
        raise BuddyError("desktop observation is missing its grounded description")
    if not isinstance(data.get("screenshot"), dict) or "image_b64" in data["screenshot"]:
        raise BuddyError("desktop observation returned invalid screenshot metadata")
    return {"ok": True, **data}


def command(action, params, timeout_ms=15000, endpoint=ENDPOINT, command_id=None):
    if not isinstance(action, str) or not action.strip() or len(action.encode("utf-8")) > 64:
        raise BuddyError("action must be a non-empty string of at most 64 UTF-8 bytes")
    if not isinstance(params, dict):
        raise BuddyError("params must be a JSON object")
    if type(timeout_ms) is not int or not 500 <= timeout_ms <= 60000:
        raise BuddyError("timeout_ms must be an integer between 500 and 60000")
    if command_id is not None and (not isinstance(command_id, str) or not command_id.strip() or len(command_id.encode("utf-8")) > 128):
        raise BuddyError("id must be a non-empty string of at most 128 UTF-8 bytes")
    request_id = command_id or str(uuid.uuid4())
    payload = {"id": request_id, "action": action, "params": params, "timeout_ms": timeout_ms}
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # Do not route device localhost through ambient HTTP proxy configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_ms / 1000 + 10) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            failure = json.loads(exc.read(8192))
            detail = failure.get("message") if isinstance(failure, dict) else None
        except (ValueError, OSError):
            detail = None
        raise BuddyError(f"device HTTP {exc.code}: {detail or 'request failed'}; action outcome unconfirmed, inspect before retry") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BuddyError("cannot reach device Buddy API or request timed out; action outcome unconfirmed, inspect before retry") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BuddyError("Buddy response exceeds 24 MiB; reduce screenshot scale")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BuddyError("device returned invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise BuddyError("device returned an invalid response envelope")
    if type(envelope.get("status")) is not int or envelope["status"] != 1:
        raise BuddyError(str(envelope.get("message") or "device rejected command"))
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise BuddyError("device response is missing Buddy result")
    if data.get("id") != request_id:
        raise BuddyError("Buddy response ID mismatch; action outcome unconfirmed")
    if data.get("ok") is not True:
        raise BuddyError(str(data.get("error") or "Buddy rejected command"))
    if not isinstance(data.get("result"), dict):
        raise BuddyError("Buddy returned an invalid result object")
    return data


def prune_captures(directory, current_path):
    """Prune only this helper's verified image/metadata pairs in one private task directory."""
    pairs = []
    for path in directory.glob("screen-*.jpg"):
        metadata = path.with_suffix(".json")
        try:
            image_stat, meta_stat = path.lstat(), metadata.lstat()
            if not stat.S_ISREG(image_stat.st_mode) or not stat.S_ISREG(meta_stat.st_mode):
                continue
            if image_stat.st_uid != os.getuid() or meta_stat.st_uid != os.getuid() or meta_stat.st_size > 65536:
                continue
            saved = json.loads(metadata.read_text(encoding="utf-8"))
            result = saved.get("result", {})
            if result.get("local_image_path") != str(path) or result.get("capture_dir") != str(directory):
                continue
            pairs.append((path == current_path, image_stat.st_mtime_ns, path, metadata))
        except (OSError, ValueError, AttributeError):
            continue
    pairs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _, _, path, metadata in pairs[MAX_CAPTURES:]:
        # Never follow symlinks while deleting; unlink affects only the directory entry.
        path.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)


def save_screenshot(data, output_dir=None):
    """Remove transport base64 and write image plus metadata on the device."""
    result = dict(data["result"])
    encoded = result.pop("image_b64", None)
    if result.get("mime") != "image/jpeg" or not isinstance(encoded, str):
        raise BuddyError("screenshot needs image/jpeg and image_b64; request return_format=base64")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BuddyError("screenshot contains invalid base64") from exc
    if not content.startswith(b"\xff\xd8\xff") or not content.endswith(b"\xff\xd9"):
        raise BuddyError("screenshot is not a complete JPEG")
    if any(type(result.get(key)) is not int or result[key] <= 0 for key in ("width", "height")):
        raise BuddyError("screenshot dimensions are missing or invalid")
    if result.get("bytes") != len(content):
        raise BuddyError("screenshot byte count mismatch")
    directory = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="autonomous-buddy-task-"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise BuddyError("screenshot output directory must be a private, owned directory (0700), not a symlink")
    directory = directory.resolve()
    # Unique files prevent a later capture from silently replacing an observed image.
    descriptor, name = tempfile.mkstemp(prefix="screen-", suffix=".jpg", dir=directory)
    image_path = Path(name).resolve()
    metadata_path = image_path.with_suffix(".json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        remote_path = result.pop("path", None)
        result.update({"local_image_path": str(image_path), "mac_image_path": remote_path,
                       "capture_dir": str(directory),
                       "metadata_path": str(metadata_path)})
        output = dict(data, result=result)
        descriptor = os.open(metadata_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, allow_nan=False)
        prune_captures(directory, image_path)
        return output
    except BaseException:
        image_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", help="Buddy action, for example get_ui_tree or screenshot")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--params", default=None, help="JSON object; quote as shell data")
    source.add_argument("--params-file", type=Path, help="UTF-8 JSON params file (avoids shell interpolation)")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--id", help="Unique command ID, retained for cancellation; never reuse IDs")
    parser.add_argument("--question", help="Required for observe: ask about a fresh Mac screenshot")
    parser.add_argument("--output-dir", type=Path, help="Device-local screenshot directory")
    args = parser.parse_args(argv)
    try:
        raw = args.params_file.read_text(encoding="utf-8") if args.params_file else (args.params or "{}")
        params = json.loads(raw)
        if not isinstance(params, dict):
            raise BuddyError("params must be a JSON object")
        if args.action == "observe":
            if args.id or args.output_dir or args.timeout_ms != 15000:
                raise BuddyError("observe uses a fixed 90-second HTTP timeout and does not accept --id or --output-dir")
            print(json.dumps(observe(args.question, params), ensure_ascii=False, allow_nan=False))
            return 0
        if args.question is not None:
            raise BuddyError("--question is only supported by observe")
        if args.action == "screenshot":
            params["return_format"] = "base64"
        data = command(args.action, params, args.timeout_ms, command_id=args.id)
        if args.action == "screenshot":
            data = save_screenshot(data, args.output_dir)
        print(json.dumps(data, ensure_ascii=False, allow_nan=False))
        return 0
    except (BuddyError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
