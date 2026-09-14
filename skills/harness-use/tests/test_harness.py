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
        if kind in ('turn.send', 'question.answer'):
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

    def test_explicit_new_target_overrides_retained_agent_and_owns_followup(self):
        with patch.object(harness, 'request', self.request):
            harness.run('send', {'agentId': 'a', 'text': 'first task'}, self.path)
            harness.run('send', {'agent': 'Other', 'text': 'review the first task'}, self.path)
            harness.run('send', {'text': 'add tests for that review'}, self.path)
        self.assertEqual([m['agentId'] for m in self.mutations], ['a', 'b', 'b'])

    def test_candidate_inspection_does_not_change_followup_target(self):
        with patch.object(harness, 'request', self.request):
            harness.run('select', {'agentId': 'a'}, self.path)
            harness.run('list', {}, self.path)
            harness.run('status', {'agentId': 'b'}, self.path)
            harness.run('recap', {'agentId': 'b', 'n': 1}, self.path)
            self.assertEqual(self.mutations, [])
            harness.run('send', {'text': 'continue the original task'}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'a')

    def test_missing_explicit_target_never_falls_back_to_retained_agent(self):
        with patch.object(harness, 'request', self.request):
            harness.run('select', {'agentId': 'a'}, self.path)
            with self.assertRaises(ValueError):
                harness.run('send', {'agent': 'Missing', 'text': 'review'}, self.path)
        self.assertEqual(self.mutations, [])

    def test_ambiguous_name_requires_id_before_dispatch(self):
        base = self.request

        def request(kind, **fields):
            if kind == 'agents.list':
                return {'machineId': 'machine', 'agents': [
                    {'agentId': 'a', 'name': 'Claude Code'},
                    {'agentId': 'b', 'name': 'Claude Code'},
                ]}
            return base(kind, **fields)

        with patch.object(harness, 'request', request):
            with self.assertRaises(ValueError):
                harness.run('send', {'agent': 'Claude Code', 'text': 'review'}, self.path)
            self.assertEqual(self.mutations, [])
            harness.run('send', {'agentId': 'b', 'text': 'review'}, self.path)
        self.assertEqual(self.mutations[0]['agentId'], 'b')

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
        self.uncertain = True
        with patch.object(harness, 'request', self.request), patch.object(harness, 'api', return_value={'machine_id': 'machine'}):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work'}, self.path)
            self.assertIn('pending', json.loads(self.path.read_text())['voice'])
            self.uncertain = False
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

    def test_send_passes_valid_direct_response_routing(self):
        with patch.object(harness, 'request', self.request):
            harness.run('send', {
                'agentId': 'a',
                'text': 'do work',
                'response': {'run_id': 'device-chat-42', 'channel': 'web'},
            }, self.path)
        self.assertEqual(
            self.mutations[0]['response'],
            {'run_id': 'device-chat-42', 'channel': 'web'},
        )

    def test_confirmed_response_route_cannot_send_the_same_turn_twice(self):
        response = {'run_id': 'device-chat-42', 'channel': 'web'}
        with patch.object(harness, 'request', self.request):
            harness.run('send', {'agentId': 'a', 'text': 'do work', 'response': response}, self.path)
            with self.assertRaisesRegex(ValueError, 'already has a confirmed delivery'):
                harness.run('send', {'text': 'do work again', 'response': response}, self.path)
            harness.run('send', {
                'text': 'new user turn',
                'response': {'run_id': 'device-chat-43', 'channel': 'web'},
            }, self.path)
        self.assertEqual(len(self.mutations), 2)

    def test_receipt_marks_an_uncertain_response_route_as_complete(self):
        self.uncertain = True
        response = {'run_id': 'device-chat-42', 'channel': 'web'}
        with patch.object(harness, 'request', self.request), patch.object(harness, 'api', return_value={'machine_id': 'machine'}):
            with self.assertRaises(OSError):
                harness.run('send', {'agentId': 'a', 'text': 'do work', 'response': response}, self.path)
            self.uncertain = False
            harness.run('receipt', {}, self.path)
            with self.assertRaisesRegex(ValueError, 'already has a confirmed delivery'):
                harness.run('send', {'text': 'do work again', 'response': response}, self.path)

    def test_answer_passes_valid_direct_response_routing(self):
        with patch.object(harness, 'request', self.request):
            harness.run('answer', {
                'agentId': 'a',
                'questionRequestId': 'question-42',
                'answers': {'location': 'Hanoi'},
                'response': {'run_id': 'device-chat-43', 'channel': 'voice'},
            }, self.path)
        self.assertEqual(
            self.mutations[0]['response'],
            {'run_id': 'device-chat-43', 'channel': 'voice'},
        )


if __name__ == '__main__':
    unittest.main()
