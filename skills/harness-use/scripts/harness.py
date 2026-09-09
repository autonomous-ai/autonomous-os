#!/usr/bin/env python3
"""Device-local Harness client with durable voice selection and uncertain-send protection."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

BASE = 'http://127.0.0.1:5000/api/harness'
MAX_BYTES = 3 * 1024 * 1024


def api(path, payload=None):
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    req = urllib.request.Request(BASE + path, data=raw, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=40) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Harness response exceeds limit')
    envelope = json.loads(data)
    if envelope.get('status') != 1:
        raise ValueError(str(envelope.get('message') or 'Harness request failed'))
    return envelope.get('data')


def request(kind, **fields):
    return api('/request', {'type': kind, 'requestId': str(uuid.uuid4()), **fields})


def save(path, value):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.harness-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(action, params, path=None):
    if not isinstance(params, dict):
        raise ValueError('Parameters must be an object')
    path = Path(path or os.environ.get('HARNESS_VOICE_STATE', str(Path.home() / '.local/state/autonomous/harness-voice.json')))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conversation = params.get('conversation_id', 'voice')
    if not isinstance(conversation, str) or not 1 <= len(conversation) <= 128:
        raise ValueError('Invalid conversation_id')
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        if conversation not in state and len(state) >= 64:
            raise ValueError('Conversation state limit reached; reuse the stable channel conversation ID')
        context = state.setdefault(conversation, {})
        if action == 'resolve':
            if params.get('resolution') != 'do_not_retry':
                raise ValueError('Explicit do_not_retry resolution required')
            context.pop('pending', None)
            save(path, state)
            return {'resolved': True, 'resent': False}
        if action == 'receipt':
            pending = context.get('pending')
            if not pending:
                return {'receipt': None, 'pending': False}
            status = api('/status')
            if status.get('machine_id') != pending['machineId']:
                raise ValueError('Pairing changed; unresolved request belongs to the previous computer')
            result = request('receipt.get', idempotencyKey=pending['idempotencyKey'])
            receipt = result.get('receipt')
            if receipt and receipt.get('state') in ('delivered', 'started', 'completed', 'rejected'):
                context.pop('pending', None)
                save(path, state)
            return result
        listing = request('agents.list')
        if listing.get('error'):
            return listing
        if action == 'list':
            return listing
        agents = listing.get('agents', [])
        machine = listing.get('machineId')
        explicit = params.get('agentId') or params.get('agent')
        selected = explicit or context.get('agentId')
        if not explicit and context.get('machineId') != machine:
            raise ValueError('Select an agent on the paired computer first')
        matches = [a for a in agents if a.get('agentId') == selected or (explicit and a.get('name') == selected)]
        if len(matches) != 1:
            raise ValueError('Select exactly one agent from list; the retained target is never replaced automatically')
        target = {'machineId': machine, 'agentId': matches[0]['agentId']}
        if action == 'select':
            context.update(target)
            save(path, state)
            return {'selected': target}
        if action in ('status', 'recap'):
            return request(action, **target, **({'n': params.get('n', 3)} if action == 'recap' else {}))
        if context.get('pending'):
            raise ValueError('A previous delivery is unresolved. Inspect receipt/status; do not resend automatically')
        if action == 'send':
            text = params.get('text')
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16384:
                raise ValueError('text must contain 1 to 16384 UTF-8 bytes')
            kind, payload = 'turn.send', {'text': text}
            response = params.get('response')
            if response is not None:
                if not isinstance(response, dict) or set(response) != {'run_id', 'channel'}:
                    raise ValueError('response must contain only run_id and channel')
                if not isinstance(response['run_id'], str) or not response['run_id'] or len(response['run_id']) > 128:
                    raise ValueError('response run_id is invalid')
                if response['channel'] not in ('voice', 'web'):
                    raise ValueError('response channel must be voice or web')
                payload['response'] = response
        elif action == 'stop':
            kind, payload = 'turn.stop', {}
        elif action == 'answer':
            if not isinstance(params.get('questionRequestId'), str) or not isinstance(params.get('answers'), dict):
                raise ValueError('questionRequestId and answers required')
            kind, payload = 'question.answer', {'questionRequestId': params['questionRequestId'], 'answers': params['answers']}
        else:
            raise ValueError('Unknown action')
        key = str(uuid.uuid4())
        context.update(target)
        context['pending'] = {**target, 'idempotencyKey': key, 'operation': kind}
        save(path, state)
        result = request(kind, **target, idempotencyKey=key, **payload)
        receipt = result.get('receipt')
        if receipt and receipt.get('state') in ('delivered', 'started', 'completed', 'rejected'):
            context.pop('pending', None)
        elif result.get('error', {}).get('code') in {
            'INVALID_REQUEST', 'UNSUPPORTED_CAPABILITY', 'MISSING_TARGET', 'MACHINE_MISMATCH',
            'AGENT_NOT_FOUND', 'QUESTION_STALE', 'PAYLOAD_TOO_LARGE', 'RATE_LIMITED', 'BACKPRESSURE',
        }:
            # Only explicit pre-dispatch refusals resolve uncertainty. INTERNAL/REVOKED may be late.
            context.pop('pending', None)
        save(path, state)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['list', 'select', 'send', 'stop', 'status', 'recap', 'answer', 'receipt', 'resolve'])
    parser.add_argument('params', nargs='?', default='{}')
    args = parser.parse_args()
    try:
        result = run(args.action, json.loads(sys.stdin.read() if args.params == '-' else args.params))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, urllib.error.URLError) as error:
        print(json.dumps({'error': str(error), 'note': 'If a mutation was reserved, delivery remains unresolved. Inspect receipt before sending again.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
