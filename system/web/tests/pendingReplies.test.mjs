import assert from "node:assert/strict";
import test from "node:test";
import { pendingReplyKey, replayedReply } from "../src/pages/monitor/chat/pendingReplies.ts";

const flow = (runId, node, data = {}) => ({ type: "flow_event", runId, detail: { node, data } });

test("accepting a POST starts recovery even though sending was already true", () => {
  const bubble = { role: "agent", pending: true };
  assert.equal(pendingReplyKey([{ messages: [bubble] }]), "[]");
  assert.equal(pendingReplyKey([{ messages: [{ ...bubble, runId: "accepted" }] }]), '["accepted"]');
});

test("switching conversations keeps both pending replies recoverable", () => {
  const first = { messages: [{ role: "agent", pending: true, runId: "first" }] };
  const second = { messages: [{ role: "agent", pending: true, runId: "second" }] };
  assert.equal(pendingReplyKey([first, second]), pendingReplyKey([second, first]));
  const events = [flow("second", "tts_suppressed", { text: "Second answer" }), flow("first", "tts_suppressed", { text: "First answer" })];
  assert.equal(replayedReply(events, "first"), "First answer");
  assert.equal(replayedReply(events, "second"), "Second answer");
});

test("completed and explicitly stopped bubbles are no longer polled", () => {
  assert.equal(pendingReplyKey([{ messages: [
    { role: "agent", pending: false, runId: "stopped" },
    { role: "agent", runId: "done" },
    { role: "user", pending: true, runId: "input" },
  ] }]), "[]");
});

test("replay uses complete reply and ignores partial audio and unrelated runs", () => {
  const events = [
    flow("mine", "tts_send", { text: "rest", full_text: "Full answer including first sentence." }),
    flow("mine", "tts_stream_send", { text: "First sentence" }),
    flow("other", "tts_suppressed", { text: "Wrong conversation" }),
  ];
  assert.equal(replayedReply(events, "mine"), "Full answer including first sentence.");
  assert.equal(replayedReply([flow("mine", "agent_last_token", { text: "Handoff" })], "mine"), undefined);
});

test("Harness final and intentional silence recover without live SSE", () => {
  assert.equal(replayedReply([flow("harness", "harness_response", { text: "Harness result" })], "harness"), "Harness result");
  assert.equal(replayedReply([flow("silent", "no_reply")], "silent"), "…");
});
