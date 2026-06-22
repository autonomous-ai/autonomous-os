import http from "http";

const handler = async (event: any): Promise<void> => {
  if (event.type !== "message" || event.action !== "preprocessed") return;

  const ctx = event.context;
  const text: string = ctx?.bodyForAgent ?? ctx?.body ?? "";

  // Skip passive sensing — these events should not flip the lamp into
  // "thinking" because the agent often decides NO_REPLY, which would leave
  // the lamp stuck on "thinking" until the next event. Skill-driven emotion
  // calls handle these paths when a real reaction is warranted.
  if (!text.trim()) return;
  if (
    text.startsWith("[sensing:") ||
    text.startsWith("[activity]") ||
    text.startsWith("[emotion]") ||
    text.startsWith("[speech_emotion]")
  ) {
    return;
  }

  // Realtime agent already handled & spoke this turn. os-server replays it to
  // the main agent (`voice_agent_handled`) ONLY to absorb memory/mood — TTS is
  // suppressed and no real reply/emotion follows. Firing "thinking" here would
  // leave the lamp stuck on the thinking face *after* the turn already finished
  // (the POST lands a few hundred ms after the spoken reply, with nothing to
  // overwrite it). The realtime path sets its own face via express_emotion.
  if (text.includes("[HANDLED]")) return;

  const req = http.request({
    hostname: "127.0.0.1",
    port: 5001,
    path: "/emotion",
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  req.on("error", () => {});
  // TODO: differentiate emotion by context — "listening" when voice/mic input (user still speaking),
  // "acknowledge" for quick command confirmations, "thinking" for text/processed messages (current default)
  req.write(JSON.stringify({ emotion: "thinking", intensity: 0.7 }));
  req.end();
};

export default handler;
