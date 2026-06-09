import http from "http";

const handler = async (event: any): Promise<void> => {
  if (event.type !== "message" || event.action !== "preprocessed") return;

  const ctx = event.context;
  const text: string = ctx?.bodyForAgent ?? ctx?.body ?? "";

  // Skip sensing events — Lamp sets busy proactively in sendChat for those
  if (text.startsWith("[sensing:") || !text.trim()) return;

  // Skip OpenClaw heartbeat / memory-flush turns. These runs do NOT emit
  // lifecycle.end SSE, so if we set busy=true here Lamp wedges for the full
  // 5-min busyTTL (see docs/debug/busy-stuck.md).
  //
  // Detected by body content: OpenClaw heartbeat prompts always end with
  // the literal sentinel "HEARTBEAT_OK" (see HEARTBEAT_PROMPT in the
  // runtime: `${HEARTBEAT_CONTEXT_PROMPT} If nothing needs attention, reply
  // HEARTBEAT_OK.`). Earlier attempts at field-based detection
  // (channelId/messageChannel/target/isHeartbeat) failed because the
  // message:preprocessed event.context does not expose any of those at the
  // hook layer. Lamp side already uses the same string match — see
  // `handler_events.go:689 isHeartbeatRun`.
  if (text.includes("HEARTBEAT_OK")) return;

  const req = http.request({
    hostname: "127.0.0.1",
    port: 5000,
    path: "/api/openclaw/busy",
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  req.on("error", () => {});
  req.end();
};

export default handler;