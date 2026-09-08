"""Behavior tests for retained target and uncertain-send protection; no network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('harness_skill', Path(__file__).parents[1] / 'scripts/harness.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class HarnessSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'voice.json'
        self.mutations = []
        self.uncertain = False

    def request(self, kind, **fields):
        if kind == 'agents.list':
            return {'machineId': 'machine', 'agents': [{'agentId': 'a', 'name': 'Project'}, {'agentId': 'b', 'name': 'Other'}]}
        if kind == 'turn.send':
            self.mutations.append(fields)
            if self.uncertain:
                raise OSError('response lost')
            return {'receipt': {'state': 'queued'}}
        if kind == 'receipt.get':
            return {'receipt': {'state': 'started'}}
        return {'state': 'running', **fields}

    def test_no_implicit_target(self):
        with patch.object(harness, 'request', self.request):
            with self.assertRaises(ValueError):
                harness.run('send', {'text': 'do work'}, self.path)
            self.assertEqual(self.mutations, [])

    def test_uncertain_send_is_persisted_and_never_replayed(self):
        self.uncertain = True
        with patch.object(harness, 'request', self.request):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])
            with self.assertRaises(ValueError):
                harness.run('send', {'text': 'try again'}, self.path)
            status = harness.run('status', {}, self.path)
            self.assertEqual(status['agentId'], 'a')
            self.assertEqual(len(self.mutations), 1)

    def test_receipt_resolves_pending_without_a_second_send(self):
        with patch.object(harness, 'request', self.request), patch.object(harness, 'api', return_value={'machine_id': 'machine'}):
            harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])
            harness.run('receipt', {}, self.path)
            self.assertNotIn('pending', json.loads(self.path.read_text())['voice'])
            harness.run('send', {'text': 'follow up'}, self.path)
            self.assertEqual([m['agentId'] for m in self.mutations], ['a', 'a'])

    def test_internal_error_retains_uncertainty(self):
        base = self.request
        def request(kind, **fields):
            return {'error': {'code': 'INTERNAL'}} if kind == 'turn.send' else base(kind, **fields)
        with patch.object(harness, 'request', request):
            harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])


if __name__ == '__main__':
    unittest.main()
