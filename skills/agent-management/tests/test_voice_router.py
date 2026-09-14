"""Exercise durable voice routing through the real HTTP command serializer."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = load("voice_router")
client = load("buddy_agents")


class VoiceRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.json"
        self.calls = []
        self.fail_send = False
        self.reject_send = False
        self.snapshot = {
            "projects": [{"id": "p1", "name": "autonomous"}],
            "projectWorktrees": {"p1": [{"path": "/repo", "branch": "main"},
                                            {"path": "/feature", "branch": "feature"}]},
            "sessions": [
                {"id": "s1", "projectId": "p1", "worktreePath": "/repo", "provider": "codex"},
                {"id": "s2", "projectId": "p1", "worktreePath": "/feature", "provider": "claude"},
                {"id": "shell", "projectId": "p1", "worktreePath": "/feature", "provider": "terminal"}],
            "activeContext": {"projectId": "p1", "worktreePath": "/feature", "sessionId": "s2"},
            "providers": [{"id": "codex", "available": True}, {"id": "claude", "available": True}],
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(body)
                action, params = body["action"], body["params"]
                if action == "agent.list":
                    value = copy.deepcopy(owner.snapshot)
                elif action == "agent.create":
                    value = {"id": "created", "projectId": params["project_id"],
                             "worktreePath": params["worktree_path"], "provider": params["provider"]}
                    owner.snapshot["sessions"].append(value)
                elif action == "agent.send" and owner.fail_send:
                    self.close_connection = True
                    return
                else:
                    value = {"accepted": True, "sessionId": params["session_id"]}
                data = {"ok": False, "error": "Agent busy"} if action == "agent.send" and owner.reject_send else {"ok": True, "result": value}
                payload = json.dumps({"status": 1, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        endpoint = "http://127.0.0.1:%d/api/buddy/command" % self.server.server_port
        self.command = lambda action, params: client.command(action, params, endpoint)
        self.voice = router.VoiceRouter(self.command, self.path)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def send(self, request="r1", **kwargs):
        return self.voice.run({"prompt": "Thêm test cho reconnect", "request_id": request, **kwargs})

    def sent(self):
        return [c["params"] for c in self.calls if c["action"] == "agent.send"]

    def focus_main(self):
        self.snapshot["activeContext"] = {"projectId": "p1", "worktreePath": "/repo", "sessionId": "s1"}

    def test_active_split_then_persisted_followup_survives_focus_and_restart(self):
        first = self.send()
        self.assertEqual(first["target"]["sessionId"], "s2")
        self.focus_main()
        self.voice = router.VoiceRouter(self.command, self.path)
        self.send("r2")
        self.assertEqual([s["session_id"] for s in self.sent()], ["s2", "s2"])
        self.assertEqual(self.sent()[1]["prompt"], "Thêm test cho reconnect")
        self.assertEqual(sum(c["action"] == "agent.list" for c in self.calls), 2)

    def test_current_means_focused_session_not_previous_voice_target(self):
        self.send()
        self.focus_main()
        self.send("r2", target="current", prompt="Print hello")
        self.send("r3", target="previous")
        self.assertEqual([s["session_id"] for s in self.sent()], ["s2", "s1", "s1"])
        self.assertEqual(self.sent()[1]["prompt"], "Print hello")

    def test_current_without_desktop_selection_does_not_fall_back_to_saved(self):
        self.send()
        self.snapshot["activeContext"] = None
        with self.assertRaises(router.RoutingError):
            self.send("r2", target="current")
        self.assertEqual(len(self.sent()), 1)

    def test_explicit_active_switch_and_conversation_isolation(self):
        self.send()
        self.focus_main()
        self.send("r2", target="active")
        self.send("r3")
        self.assertEqual([s["session_id"] for s in self.sent()], ["s2", "s1", "s1"])
        self.snapshot["activeContext"]["sessionId"] = None
        with self.assertRaises(router.RoutingError):
            self.send("r4", conversation_id="another-user")
        self.assertEqual(len(self.sent()), 3)

    def test_ambiguous_project_or_worktree_does_not_send(self):
        for selectors in ({"project": "autonomous"}, {"project_id": "p1", "worktree": "missing"}):
            with self.subTest(selectors=selectors), self.assertRaises(router.RoutingError):
                self.send(**selectors)
        self.assertEqual(self.sent(), [])

    def test_stale_retained_session_blocks_without_falling_back_to_focus(self):
        self.send()
        self.snapshot["sessions"][1]["closed"] = True
        self.focus_main()
        with self.assertRaises(router.RoutingError):
            self.send("r2")
        self.assertEqual(len(self.sent()), 1)

    def test_shell_and_provider_mismatch_do_not_send(self):
        self.snapshot["activeContext"]["sessionId"] = "shell"
        with self.assertRaises(router.RoutingError):
            self.send()
        with self.assertRaises(router.RoutingError):
            self.send(project_id="p1", session_id="s1", provider="claude")
        self.assertEqual(self.sent(), [])

    def test_new_session_uses_selected_worktree_once_and_duplicate_is_cached(self):
        params = {"new_session": True, "provider": "codex", "target": "active"}
        first = self.send(**params)
        self.focus_main()
        self.assertEqual(self.send(**params), first)
        creates = [c["params"] for c in self.calls if c["action"] == "agent.create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["worktree_path"], "/feature")
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.sent()[0]["session_id"], "created")
        self.send("r2")
        self.assertEqual(self.sent()[1]["session_id"], "created")

    def test_reused_request_id_with_different_prompt_is_rejected(self):
        self.send()
        with self.assertRaises(router.RoutingError):
            self.voice.run({"request_id": "r1", "prompt": "Different task"})
        self.assertEqual(len(self.sent()), 1)

    def test_uncertain_delivery_survives_reload_and_never_blindly_replays(self):
        self.fail_send = True
        with self.assertRaises(client.BuddyError):
            self.send()
        self.voice = router.VoiceRouter(self.command, self.path)
        self.fail_send = False
        for request in ("r1", "r2"):
            with self.subTest(request=request), self.assertRaises(router.RoutingError):
                self.send(request)
        self.assertEqual(len(self.sent()), 1)
        status = self.voice.run({"operation": "status"})
        self.assertEqual(status["target"]["sessionId"], "s2")

    def test_status_stop_preserve_exact_selected_session(self):
        self.voice.run({"operation": "select", "project_id": "p1", "session_id": "s1"})
        for operation in ("status", "stop"):
            self.assertEqual(self.voice.run({"operation": operation})["target"]["sessionId"], "s1")
        self.assertEqual(self.sent(), [])
        self.assertEqual([c["action"] for c in self.calls][-1], "agent.stop")

    def test_definite_rejection_does_not_block_future_authorized_send(self):
        self.reject_send = True
        with self.assertRaises(client.BuddyRejected):
            self.send()
        self.reject_send = False
        self.assertFalse(self.send()["accepted"])
        self.assertEqual(len(self.sent()), 1)
        self.send("r2")
        self.assertEqual(len(self.sent()), 2)

    def test_explicit_abandon_releases_uncertain_request_without_replay(self):
        self.fail_send = True
        with self.assertRaises(client.BuddyError):
            self.send()
        self.fail_send = False
        self.assertEqual(self.voice.run({"operation": "status"})["uncertain_requests"], ["r1"])
        with self.assertRaises(router.RoutingError):
            self.voice.run({"operation": "resolve", "request_id": "r1", "resolution": "retry"})
        result = self.voice.run({"operation": "resolve", "request_id": "r1", "resolution": "do_not_retry"})
        self.assertTrue(result["abandoned"])
        self.assertEqual(len(self.sent()), 1)
        self.assertTrue(self.send()["abandoned"])
        self.send("r2")
        self.assertEqual(len(self.sent()), 2)

    def test_resolve_cannot_replace_successful_receipt(self):
        result = self.send()
        with self.assertRaises(router.RoutingError):
            self.voice.run({"operation": "resolve", "request_id": "r1", "resolution": "do_not_retry"})
        self.assertEqual(self.send(), result)
        self.assertEqual(len(self.sent()), 1)

    def test_corrupt_state_is_preserved_and_sends_nothing(self):
        self.path.write_text("broken JSON")
        with self.assertRaises(router.RoutingError):
            self.send()
        self.assertEqual(self.path.read_text(), "broken JSON")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
