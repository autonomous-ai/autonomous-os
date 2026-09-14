const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const ts = require('typescript');

// Load the pure UI reducers with the project's existing TypeScript compiler.
// This process-local loader avoids adding a browser or a test-runner dependency.
require.extensions['.ts'] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  });
  module._compile(outputText, filename);
};
const { groupIntoTurns, turnIO, turnHasOutput, deriveActiveStage, extractNodeInfo } =
  require('../src/pages/monitor/FlowSection/helpers.ts');

function event(seq, runId, node, data, type = 'flow_event') {
  return { id: String(seq), _seq: seq, runId, type,
    time: new Date(1789363300000 + seq * 1000).toISOString(),
    summary: '', detail: { node, data } };
}
function input(seq, runId, type = 'voice', route = 'harness_only') {
  return event(seq, runId, 'sensing_input', { type, route, message: `input ${runId}` }, 'flow_enter');
}
function reply(seq, runId, text = `answer ${runId}`) {
  return event(seq, runId, 'harness_response', { text, run_id: runId });
}

test('persisted Harness response closes the turn and restores output without live SSE or TTS', () => {
  const events = [input(1, 'a'), event(2, 'a', 'sensing_input', { route: 'harness_only' }, 'flow_exit'), reply(3, 'a')];
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.path, 'harness');
  assert.equal(turn.status, 'done');
  assert.equal(turn.endTime, events[2].time);
  assert.equal(turnIO(turn).output, 'answer a');
  assert.equal(turnHasOutput(turn), true);
  assert.equal(deriveActiveStage(events), 'agent_response');
  assert.ok(extractNodeInfo(events).agent_response.some((text) => text.includes('answer a')));
});

test('accepted input stays active until its own nonempty response', () => {
  const [turn] = groupIntoTurns([input(1, 'a'), event(2, 'a', 'sensing_input', {}, 'flow_exit'), reply(3, 'a', '  ')]);
  assert.equal(turn.path, 'harness');
  assert.equal(turn.status, 'active');
  assert.equal(turnHasOutput(turn), false);
});

test('interleaved replies attach to their original runs, leaving unanswered inputs active', () => {
  const events = [input(1, 'a'), input(2, 'b', 'voice_followup'), input(3, 'c'), reply(4, 'b'), reply(5, 'a')];
  const turns = groupIntoTurns(events);
  assert.equal(turns.length, 3);
  for (const turn of turns) {
    assert.equal(turn.path, 'harness');
    assert.equal(turn.status, turn.runId === 'c' ? 'active' : 'done');
    if (turn.runId !== 'c') assert.equal(turnIO(turn).output, `answer ${turn.runId}`);
  }
});

test('an orphan response never closes or supplies output to another input', () => {
  const turns = groupIntoTurns([input(1, 'a'), reply(2, 'unknown')]);
  const turn = turns.find((item) => item.runId === 'a');
  assert.equal(turn.status, 'active');
  assert.ok(!turnIO(turn).output);
  assert.equal(turns.length, 2);
});

test('main-agent routing stays unchanged and delegated Harness final also ends its own run', () => {
  const events = [input(1, 'main', 'voice', undefined), event(2, 'main', 'chat_send', { message: 'hello' }), reply(3, 'main')];
  delete events[0].detail.data.route;
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.path, 'agent');
  assert.equal(turn.status, 'done');
  assert.equal(turnIO(turn).output, 'answer main');
});

test('a Harness final preserves an existing lifecycle error', () => {
  const failure = event(2, 'a', 'lifecycle_error', {});
  failure.type = 'lifecycle';
  failure.phase = 'error';
  const [turn] = groupIntoTurns([input(1, 'a'), failure, reply(3, 'a')]);
  assert.equal(turn.status, 'error');
  assert.equal(turnIO(turn).output, 'answer a');
});

function historyMessage(source = 'harness') {
  return '[skills: input-branching]\n[external-context] ' + JSON.stringify({ source, agent_name: 'Coder' }) +
    '\nHistory only. NO_REPLY.\n[HANDLED] ' + JSON.stringify('Open Chrome\nand search') +
    '\n[REPLY] ' + JSON.stringify('A tab is open.');
}

test('history sync displays the exchange without instruction or metadata wrappers', () => {
  const id = 'device-chat-context-test';
  const events = [event(1, id, 'chat_input', { message: historyMessage().slice(0, 140) }),
    event(2, id, 'chat_send', { message: historyMessage() }),
    event(3, id, 'lifecycle_end', {})];
  const [turn] = groupIntoTurns(events);
  assert.equal(turn.type, 'history_sync');
  assert.equal(turn.status, 'done');
  assert.equal(turnIO(turn).input, 'Open Chrome\nand search');
  assert.equal(turnIO(turn).output, 'A tab is open.');
});

test('history parsing is source-neutral and does not close a pending sync', () => {
  const id = 'device-chat-context-other';
  const [turn] = groupIntoTurns([event(1, id, 'chat_input', { message: historyMessage('another-device') })]);
  assert.equal(turn.type, 'history_sync');
  assert.equal(turn.status, 'active');
  assert.equal(turnIO(turn).output, 'A tab is open.');
});

test('normal turns and malformed context envelopes do not masquerade as history sync', () => {
  for (const [id, message] of [['device-chat-user', historyMessage()],
    ['device-chat-context-broken', historyMessage().slice(0, 150)]]) {
    const [turn] = groupIntoTurns([event(1, id, 'chat_input', { message })]);
    assert.ok(turn);
    assert.notEqual(turn.type, 'history_sync');
  }
});
