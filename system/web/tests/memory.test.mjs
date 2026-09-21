import assert from "node:assert/strict";
import test from "node:test";
import { turnMemoryState } from "../src/pages/monitor/FlowSection/memory.ts";

const turn = (...events) => ({ id: "t", startTime: "", type: "chat", path: "agent", status: "done", events });
const flow = (node, data = {}) => ({ type: "flow_event", detail: { node, data } });

test("a quarantine carries the runtime it happened in", () => {
  const state = turnMemoryState(turn(
    flow("lifecycle_start", { memory: { "USER.md": { size: 251, sha8: "a1b2c3d4" } } }),
    flow("memory_changed", { file: "USER.md", runtime: "hermes", quarantined: 1, reasons: ["free-prose"], execute: true }),
  ));
  assert.equal(state.changed[0].runtime, "hermes");
  assert.equal(state.changed[0].quarantined, 1);
  assert.equal(state.files["USER.md"].size, 251);
});

test("an os-server that sends no runtime yields an empty name, not undefined", () => {
  const state = turnMemoryState(turn(flow("memory_changed", { file: "MEMORY.md", quarantined: 0 })));
  assert.equal(state.changed[0].runtime, "");
  // Missing `execute` means an older os-server that always executed.
  assert.equal(state.changed[0].execute, true);
});

test("a turn with neither fingerprint nor change has no memory state", () => {
  assert.equal(turnMemoryState(turn(flow("lifecycle_start", {}))), null);
});
