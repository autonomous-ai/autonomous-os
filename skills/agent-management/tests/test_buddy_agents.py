import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("buddy_agents", Path(__file__).parents[1] / "scripts" / "buddy_agents.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ClientTests(unittest.TestCase):
    def test_invalid_inputs_do_not_send(self):
        for action, params in [("bad", {}), ("list", []), ("send", {}),
                               ("session", {"project_id": "p", "session_id": "s", "after_seq": True})]:
            with self.subTest(action=action, params=params), patch.object(client.urllib.request, "build_opener") as opener:
                with self.assertRaises(client.BuddyError):
                    client.command(action, params)
                opener.assert_not_called()

    def test_response_and_errors(self):
        for body, succeeds in [({"status": 1, "data": {"ok": True, "result": {"sessions": []}}}, True),
                               ({"status": True, "data": {}}, False),
                               ({"status": 1, "data": {"ok": False, "error": "wrong session"}}, False),
                               ([], False), ("bad json", False)]:
            with self.subTest(body=body), patch.object(client.urllib.request, "build_opener") as build:
                response = MagicMock()
                response.read.return_value = (body if isinstance(body, str) else json.dumps(body)).encode()
                build.return_value.open.return_value.__enter__.return_value = response
                if succeeds:
                    self.assertEqual(client.command("list", {}), {"sessions": []})
                else:
                    with self.assertRaises(client.BuddyError):
                        client.command("list", {})
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_timeout_is_not_retried(self):
        with patch.object(client.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError()
            with self.assertRaisesRegex(client.BuddyError, "delivery uncertain"):
                client.command("list", {})
            self.assertEqual(build.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
