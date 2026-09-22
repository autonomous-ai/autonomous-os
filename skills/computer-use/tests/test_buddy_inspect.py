"""Observation compaction and preflight tests; no desktop/network access."""
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("buddy_inspect", Path(__file__).parents[1] / "scripts/buddy_inspect.py")
inspect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspect)


def tree(nodes, **extra):
    return dict(snapshot_id="snapshot", expires_in_ms=30000, frontmost=True,
                truncated=False, nodes=nodes, **extra)


def node(ref, **extra):
    return dict(ref=ref, role="AXStaticText", secure=False, **extra)


class InspectTest(unittest.TestCase):
    def test_preserves_evidence_and_marks_missing_content(self):
        original = tree([node("root"), node("value", parent_ref="root", value="4"),
                         node("button", title="2", actions=["press"], enabled=True),
                         dict(ref="menu", role="AXMenu", secure=False, title="Menu"),
                         node("entry", parent_ref="menu", title="Delete")])
        result = inspect.compact_tree(original)
        self.assertEqual([n["ref"] for n in result["items"]], ["value", "button"])
        self.assertEqual(result["items"][0]["value"], "4")
        self.assertEqual(result["snapshot_id"], "snapshot")
        self.assertIn("compact_output_omits_content", result["issues"])
        self.assertEqual(len(original["nodes"]), 5)

    def test_secure_and_unknown_ancestors_hide_descendants(self):
        for privacy in (True, None):
            parent = dict(ref="parent", role="AXGroup", secure=privacy, value="secret")
            result = inspect.compact_tree(tree([parent, node("child", parent_ref="parent", value="password")]))
            self.assertEqual(result["items"], [])

    def test_invalid_ancestry_rejected(self):
        for nodes in ([node("a", parent_ref="missing")], [node("a", parent_ref="b"), node("b", parent_ref="a")], [node("a"), node("a")]):
            with self.assertRaises(ValueError):
                inspect.compact_tree(tree(nodes))

    def test_bounded_output_never_claims_completeness(self):
        original = tree([node(str(i), value="x" * 500) for i in range(150)])
        original.update(frontmost=False, truncated=True)
        result = inspect.compact_tree(original)
        self.assertEqual(len(result["items"]), 120)
        self.assertTrue(result["text_clipped"])
        self.assertEqual(len(result["items"][0]["value"]), 240)
        self.assertIn("app_not_frontmost", result["issues"])
        self.assertIn("tree_incomplete", result["issues"])

    def test_permission_and_pause_block_tree(self):
        for desktop in ({"paused": True, "accessibility": True}, {"paused": False, "accessibility": False}, {}):
            calls = []
            def command(action, params):
                calls.append(action)
                return {"result": desktop}
            self.assertFalse(inspect.inspect_ui({}, command)["ok"])
            self.assertEqual(calls, ["desktop_info"])

    def test_missing_screen_permission_still_allows_accessibility(self):
        calls = []
        def command(action, params):
            calls.append((action, params))
            if action == "desktop_info":
                return {"result": {"paused": False, "accessibility": True, "screen_recording": False}}
            return {"result": tree([node("a", value="Today")])}
        result = inspect.inspect_ui({"app": "Calendar"}, command)
        self.assertTrue(result["ok"])
        self.assertIn("screen_recording_unavailable", result["observation"]["issues"])
        self.assertEqual([c[0] for c in calls], ["desktop_info", "get_ui_tree"])
        self.assertEqual(calls[1][1], {"app": "Calendar", "max_nodes": 500, "max_depth": 12})

    def test_bad_params_make_no_call(self):
        for params in ({"app": ""}, {"goal": "invent a filter"},
                       {"window_id": True}, {"window_id": 0}, {"window_id": -1},
                       {"window_id": "123"}, {"window_id": 1.5}):
            with self.assertRaises(ValueError):
                inspect.inspect_ui(params, lambda *args: self.fail("unexpected native call"))

    def test_cua_preserves_upstream_observation_without_native_permission_or_jev(self):
        observation = dict(backend="cua", pid=123, window_id=456,
                           snapshot_id="local-token", cua_snapshot_id="upstream-token",
                           elements=[{"element_index": 1, "label": "Equals"}],
                           tree_markdown="AXStaticText: 4", elements_complete=True,
                           expires_in_ms=30000)
        calls = []
        def command(action, params):
            calls.append((action, params))
            if action == "desktop_info":
                return {"result": {"paused": False, "accessibility": False,
                                   "screen_recording": False,
                                   "cua": {"installed": True, "enabled": True}}}
            return {"result": observation}
        result = inspect.inspect_ui({"app": "Calculator", "window_id": 456}, command)
        self.assertTrue(result["ok"])
        self.assertIs(result["observation"], observation)
        self.assertEqual(calls, [("desktop_info", {}),
                                 ("cua_observe", {"app": "Calculator", "window_id": 456})])

    def test_cua_window_selection_is_preserved_without_retry(self):
        observation = dict(backend="cua", pid=123, requires_window_selection=True,
                           windows=[{"window_id": 456}, {"window_id": 789}])
        calls = []
        def command(action, params):
            calls.append(action)
            if action == "desktop_info":
                return {"result": {"paused": False, "cua": {"installed": True, "enabled": True}}}
            return {"result": observation}
        result = inspect.inspect_ui({}, command)
        self.assertIs(result["observation"], observation)
        self.assertNotIn("snapshot_id", result["observation"])
        self.assertEqual(calls, ["desktop_info", "cua_observe"])

    def test_cua_failure_never_falls_back(self):
        for error in (TimeoutError("timeout"), PermissionError("permission denied"), RuntimeError("driver stopped")):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": False, "accessibility": True,
                                       "cua": {"installed": True, "enabled": True}}}
                raise error
            with self.assertRaises(type(error)):
                inspect.inspect_ui({}, command)
            self.assertEqual(calls, ["desktop_info", "cua_observe"])

    def test_native_fallback_only_when_cua_disabled_or_absent(self):
        for cua in ({"installed": True, "enabled": False},
                    {"installed": False, "enabled": True}, None):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": False, "accessibility": True, "cua": cua}}
                return {"result": tree([node("value", value="4")])}
            self.assertTrue(inspect.inspect_ui({}, command)["ok"])
            self.assertEqual(calls, ["desktop_info", "get_ui_tree"])
            calls.clear()
            with self.assertRaisesRegex(ValueError, "window_id requires Cua"):
                inspect.inspect_ui({"window_id": 456}, command)
            self.assertEqual(calls, ["desktop_info"])

    def test_modes_and_incomplete_auto_replace_snapshot_once_on_each_route(self):
        for backend in ("cua", "native"):
            for known in (None, backend):
                for mode in ("auto", "navigation", "detail"):
                    calls = []
                    observations = []
                    def command(action, params):
                        calls.append((action, params))
                        if action == "desktop_info":
                            return {"result": {"paused": False, "accessibility": True,
                                               "cua": {"installed": backend == "cua", "enabled": True}}}
                        token = "fresh" if params.get("max_depth") in (2, 4) else "old"
                        value = ({"snapshot_id": token, "elements_complete": False, "elements": [{"element_token": token}]}
                                 if backend == "cua" else dict(tree([node(token, value=token)]), truncated=True))
                        value["snapshot_id"] = token
                        observations.append(value)
                        return {"id": str(len(calls)), "result": value}
                    target = {"app": "Calendar", "mode": mode}
                    if backend == "cua":
                        target["window_id"] = 7
                    result = inspect.inspect_ui(target, command, known_backend=known)
                    expected_reads = 2 if mode == "auto" else 1
                    self.assertEqual(len(observations), expected_reads)
                    self.assertEqual(result["observation"]["snapshot_id"], "old" if mode == "detail" else "fresh")
                    self.assertEqual(result["navigation_only"], mode != "detail")
                    if mode != "detail":
                        self.assertFalse(result["content_complete"])
                        self.assertNotIn('"old"', json.dumps(result["observation"]))
                    for action, params in calls:
                        self.assertNotIn("mode", params)
                        if action != "desktop_info":
                            self.assertEqual(params["app"], "Calendar")
                            if backend == "cua":
                                self.assertEqual(params["window_id"], 7)
                    self.assertEqual(len(result["timing"]["requests"]), len(calls))

    def test_incomplete_auto_second_read_error_never_falls_back(self):
        for backend in ("cua", "native"):
            for known in (None, backend):
                calls = []
                def command(action, params):
                    calls.append(action)
                    if action == "desktop_info":
                        return {"result": {"paused": False, "accessibility": True,
                                           "cua": {"installed": backend == "cua", "enabled": True}}}
                    if params.get("max_depth") in (2, 4):
                        raise PermissionError("paused or permission revoked")
                    return {"result": {"elements_complete": False} if backend == "cua" else dict(tree([]), truncated=True)}
                with self.assertRaises(PermissionError):
                    inspect.inspect_ui({"app": "Calendar"}, command, known_backend=known)
                self.assertEqual(calls.count("cua_observe" if backend == "cua" else "get_ui_tree"), 2)
                self.assertNotIn("get_ui_tree" if backend == "cua" else "cua_observe", calls)

    def test_navigation_menu_refs_preserved_but_secure_ancestors_always_hidden(self):
        nodes = [dict(node("menu", title="Go"), role="AXMenu"),
                 dict(node("go-date", parent_ref="menu", title="Go to Date", actions=["press"], enabled=True), role="AXMenuItem"),
                 dict(node("secure-menu", title="Private"), role="AXMenu", secure=True),
                 dict(node("secret", parent_ref="secure-menu", title="Hidden", actions=["press"], enabled=True), role="AXMenuItem"),
                 dict(node("unknown-menu", title="Unknown"), role="AXMenu", secure=None),
                 node("unknown-child", parent_ref="unknown-menu", title="Hidden too")]
        for mode in ("navigation", "detail"):
            def command(action, params):
                return {"result": tree(nodes)}
            result = inspect.inspect_ui({"mode": mode}, command, known_backend="native")
            refs = [item["ref"] for item in result["observation"]["items"]]
            self.assertNotIn("secret", refs)
            self.assertNotIn("unknown-child", refs)
            if mode == "navigation":
                self.assertIn("go-date", refs)
                self.assertEqual(result["observation"]["items"][0]["parent_ref"], "menu")
                self.assertFalse(result["content_complete"])
            else:
                self.assertNotIn("go-date", refs)

    def test_navigation_prioritizes_enabled_controls_under_output_limit(self):
        original = tree([node(str(i), value="day") for i in range(140)] +
                        [node("navigate", title="Go to Date", actions=["press"], enabled=True)])
        result = inspect.compact_tree(original, navigation=True)
        self.assertEqual(result["items"][0]["ref"], "navigate")
        self.assertEqual(len(result["items"]), 120)
        self.assertEqual(result["omitted_nodes"], 21)
        self.assertIn("compact_output_omits_content", result["issues"])

    def test_cua_compaction_removes_driver_advice_preserves_unique_tree_evidence(self):
        raw = {"snapshot_id": "snapshot", "cua_snapshot_id": "upstream", "elements_complete": False,
               "elements": [{"element_token": "upstream:1", "role": "AXButton", "actions": ["AXPress"],
                             "frame": {"x": 1, "y": 2, "width": 10, "height": 10}, "secure": False}],
               "tree_markdown": "AXStaticText: unique appointment at 10:30", "truncated": True,
               "_note": "Increase max_elements to see more"}
        result = inspect.compact_cua(raw)
        self.assertNotIn("_note", result)
        self.assertIn("_note", raw)
        self.assertEqual(result, {key: value for key, value in raw.items() if key != "_note"})
        self.assertIs(result["elements"], raw["elements"])
        self.assertEqual(result["tree_markdown"], raw["tree_markdown"])

    def test_cua_note_removed_in_full_navigation_and_auto_outputs(self):
        for mode in ("auto", "detail", "navigation"):
            def command(action, params):
                return {"result": {"snapshot_id": "fresh", "elements_complete": False,
                                   "elements": [], "tree_markdown": "tree-only text",
                                   "_note": "max_elements"}}
            result = inspect.inspect_ui({"mode": mode}, command, known_backend="cua")
            self.assertNotIn("_note", result["observation"])
            self.assertEqual(result["observation"]["tree_markdown"], "tree-only text")
            self.assertFalse(result["observation"]["elements_complete"])

    def test_invalid_mode_is_rejected_before_any_command(self):
        for mode in (None, "other", {}, [], 1):
            with self.assertRaisesRegex(ValueError, "mode"):
                inspect.inspect_ui({"mode": mode}, lambda *args: self.fail("unexpected call"))

    def test_pause_blocks_cua_and_invalid_cua_result_is_rejected(self):
        for paused in (True, False):
            calls = []
            def command(action, params):
                calls.append(action)
                if action == "desktop_info":
                    return {"result": {"paused": paused,
                                       "cua": {"installed": True, "enabled": True}}}
                return {"result": None}
            if paused:
                self.assertFalse(inspect.inspect_ui({}, command)["ok"])
                self.assertEqual(calls, ["desktop_info"])
            else:
                with self.assertRaisesRegex(ValueError, "invalid Cua observation"):
                    inspect.inspect_ui({}, command)
                self.assertEqual(calls, ["desktop_info", "cua_observe"])


if __name__ == "__main__":
    unittest.main()
