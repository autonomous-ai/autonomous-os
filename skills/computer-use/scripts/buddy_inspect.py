"""Read-only desktop observations through Cua or native Accessibility."""

import time

MAX_ITEMS = 120
TEXT_LIMIT = 240


def compact_tree(tree):
    """Keep observed references and text, with explicit incompleteness markers."""
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > 500:
        raise ValueError("invalid Accessibility nodes")
    by_ref = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("ref"), str) or not node["ref"] or node["ref"] in by_ref:
            raise ValueError("invalid or duplicate Accessibility reference")
        by_ref[node["ref"]] = node
    items = []
    omitted = 0
    text_clipped = False
    for node in nodes:
        current, seen = node, set()
        hidden = False
        while current:
            ref = current["ref"]
            if ref in seen:
                raise ValueError("cyclic Accessibility ancestry")
            seen.add(ref)
            # Missing privacy markers are unknown, never permission to expose text.
            hidden |= current.get("secure") is not False or current.get("role") in ("AXMenuBar", "AXMenu", "AXMenuItem")
            parent = current.get("parent_ref")
            if not parent:
                break
            if parent not in by_ref:
                raise ValueError("missing Accessibility parent")
            current = by_ref[parent]
        if hidden:
            omitted += 1
            continue
        item = {key: node[key] for key in ("ref", "parent_ref", "role", "enabled", "focused", "actions") if key in node}
        has_text = False
        for key in ("title", "description", "value"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value[:TEXT_LIMIT]
                has_text = True
                text_clipped |= len(value) > TEXT_LIMIT
        if not has_text and not node.get("actions"):
            omitted += 1
            continue
        if len(items) >= MAX_ITEMS:
            omitted += 1
            continue
        items.append(item)
    result = {key: tree.get(key) for key in ("app", "bundle_id", "snapshot_id", "expires_in_ms", "frontmost", "truncated")}
    result.update(items=items, observed_nodes=len(nodes), omitted_nodes=omitted,
                  text_clipped=text_clipped, compact=True)
    issues = []
    if tree.get("frontmost") is not True:
        issues.append("app_not_frontmost")
    if tree.get("truncated") is not False:
        issues.append("tree_incomplete")
    if omitted or text_clipped:
        issues.append("compact_output_omits_content")
    result["issues"] = issues
    return result


def validate_inspect_params(params):
    """Validate observation input before any command, including a preceding action."""
    if not isinstance(params, dict) or set(params) - {"app", "window_id"}:
        raise ValueError("inspect params may contain only app and window_id")
    if "app" in params and (not isinstance(params["app"], str) or not params["app"].strip() or len(params["app"]) > 256):
        raise ValueError("app must contain 1–256 characters")
    if "window_id" in params and (type(params["window_id"]) is not int or params["window_id"] <= 0):
        raise ValueError("window_id must be a positive integer")


def inspect_ui(params, command, known_backend=None):
    """Select one backend, then observe once. Never activate, retry or fail over."""
    validate_inspect_params(params)
    if known_backend not in (None, "cua", "native"):
        raise ValueError("invalid known observation backend")
    if known_backend == "native" and "window_id" in params:
        raise ValueError("window_id requires Cua")
    started = time.monotonic()
    requests = []

    def measured_command(action, target):
        before = time.monotonic()
        response = command(action, target)
        requests.append({"action": action, "id": response.get("id"),
                         "elapsed_ms": round((time.monotonic() - before) * 1000, 2)})
        return response

    if known_backend is None:
        result = _inspect_ui(params, measured_command)
    else:
        # A successful typed action proves its backend. The companion still
        # enforces pause and permissions on this new observation command.
        action = "cua_observe" if known_backend == "cua" else "get_ui_tree"
        target = params if known_backend == "cua" else dict(params, max_nodes=500, max_depth=12)
        response = measured_command(action, target)
        observation = response.get("result")
        if not isinstance(observation, dict):
            raise ValueError("invalid observation result")
        if known_backend == "native":
            observation = compact_tree(observation)
        result = {"ok": True, "desktop": None, "backend": known_backend,
                  "backend_source": "successful_action", "observation": observation}

    result["timing"] = {"elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                        "requests": requests}
    return result


def _inspect_ui(params, command):
    desktop = command("desktop_info", {}).get("result")
    if not isinstance(desktop, dict):
        raise ValueError("invalid desktop information")
    state = {key: desktop.get(key) for key in ("paused", "accessibility", "screen_recording", "frontmost_app", "cua",
                                                       "capabilities", "protocol_version")}
    if desktop.get("paused") is not False:
        return {"ok": False, "reason": "desktop_paused_or_unknown", "desktop": state}
    cua = desktop.get("cua")
    if isinstance(cua, dict) and cua.get("enabled") is True and cua.get("installed") is True:
        # Cua has its own TCC identity. Buddy's AX grant cannot predict its readiness.
        response = command("cua_observe", params)
        observation = response.get("result")
        if not isinstance(observation, dict):
            raise ValueError("invalid Cua observation")
        # Preserve upstream text and tokens; static text may exist only in tree_markdown.
        # The transport and adapter bound this result; do not duplicate or truncate it.
        return {"ok": True, "desktop": state, "observation": observation}
    if "window_id" in params:
        raise ValueError("window_id requires Cua")
    if desktop.get("accessibility") is not True:
        return {"ok": False, "reason": "accessibility_unavailable", "desktop": state}
    response = command("get_ui_tree", dict(params, max_nodes=500, max_depth=12))
    tree = response.get("result")
    if not isinstance(tree, dict):
        raise ValueError("invalid Accessibility result")
    observation = compact_tree(tree)
    if desktop.get("screen_recording") is not True:
        observation["issues"].append("screen_recording_unavailable")
    return {"ok": True, "desktop": state, "observation": observation}
