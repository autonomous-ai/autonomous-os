import type { DisplayEvent } from "../types";
import type { ActiveFlowStage, Turn, NodeInfoMap } from "./types";
import { FLOW_NODES, CHANNEL_FALLBACK_MESSAGE } from "./types";

// Known external messaging channel types. Turn types matching these are channel-initiated turns.
const CHANNEL_TYPES = new Set(["telegram", "discord", "slack", "wechat", "channel"]);
function isChannelType(type: string): boolean {
  return CHANNEL_TYPES.has(type);
}

// Lumi emits motion.activity/emotion.detected/speech_emotion.detected/pose.ergo_risk
// with domain-specific prefixes ([activity]/[emotion]/[speech_emotion]/[posture])
// instead of [sensing:*] so SOUL.md's [sensing:*] rule doesn't force the sensing
// skill into context. Parsing here supports all domain-specific sensing prefixes.
const SENSING_PREFIX_RE = /^\s*\[(?:sensing:([^\]]+)|(activity|emotion|speech_emotion|posture))\]/i;

// Returns the internal sensing type ("motion.activity", "emotion.detected",
// "speech_emotion.detected", "presence.enter", …) from a message prefix, or
// null if the message doesn't start with one.
export function extractSensingType(msg: string): string | null {
  const m = msg.match(SENSING_PREFIX_RE);
  if (!m) return null;
  if (m[1]) return m[1];                                       // [sensing:<type>]
  if (m[2] === "activity") return "motion.activity";
  if (m[2] === "emotion") return "emotion.detected";
  if (m[2] === "speech_emotion") return "speech_emotion.detected";
  if (m[2] === "posture") return "pose.ergo_risk";
  return null;
}

// True if the message starts with a sensing prefix (any taxonomy, anchored).
export function hasSensingPrefix(msg: string): boolean {
  return SENSING_PREFIX_RE.test(msg);
}

// Same as hasSensingPrefix but without the ^ anchor — matches anywhere in the
// string. Useful for node-host echo detection where the prefix may be embedded.
const SENSING_PREFIX_ANYWHERE_RE = /\[(?:sensing:[^\]]+|activity|emotion|speech_emotion|posture)\]/i;
export function containsSensingPrefix(msg: string): boolean {
  return SENSING_PREFIX_ANYWHERE_RE.test(msg);
}

// PipelineRow describes one row in the OpenClaw event pipeline visualization.
// Consecutive deltas of the same stream type are merged into a single row;
// every tool call and every operational stream event (compaction/error/etc.)
// becomes its own row so rare events stand out.
export interface PipelineRow {
  /** Underlying OpenClaw stream type — drives row color/label. */
  kind: "thinking" | "assistant" | "tool" | "tool_result" | "lifecycle_start" | "lifecycle_end" | "compaction" | "error" | "other";
  label: string;        // e.g. "thinking", "tool · bash", "lifecycle:start"
  detail?: string;      // optional secondary text (tool args summary, error msg)
  startMs: number;      // first event timestamp
  endMs: number;        // last event timestamp (== startMs for one-shot rows)
  durationMs: number;   // endMs - startMs
  chunks: number;       // number of merged source events (1 for one-shot rows)
  chars: number;        // total streamed text length (0 for non-text events)
}

// Aggregate the raw turn events into a sequential list of pipeline rows. The
// caller is expected to pass the FULL turn events (already filtered by
// runId), in chronological order. Output preserves the original order so the
// UI can render top-to-bottom = first-to-last.
//
// Aggregation rules:
// - Consecutive `thinking` deltas → one row "thinking" (chunks/chars/dur).
// - Consecutive `assistant_delta` events → one row "assistant".
// - Each `tool_call` (any phase) → its own row labeled "tool · <name>". A
//   `result` phase is emitted as a "tool_result" row attached to the
//   preceding tool start (linked by run_id+name).
// - `flow_event:lifecycle_start` / `lifecycle_end` → one-shot rows.
// - Operational streams (compaction, error, item, plan, approval,
//   command_output, patch) → one row each, kind="compaction"|"error"|"other".
// - Other flow events (chat_send, hw_*, tts_send, …) are NOT aggregated
//   into the pipeline — they belong to the surrounding flow nodes
//   (Agent Call, Lumi Hook, etc.) and would clutter the pipeline.
export function aggregateEvents(events: DisplayEvent[]): PipelineRow[] {
  const rows: PipelineRow[] = [];

  const flowEventNode = (ev: DisplayEvent): string | undefined => {
    if (ev.type !== "flow_event") return undefined;
    const d = ev.detail as Record<string, any> | undefined;
    return d?.node;
  };

  const ts = (ev: DisplayEvent) => new Date(ev.time).getTime();
  const deltaText = (ev: DisplayEvent): string => {
    const d = ev.detail as Record<string, any> | undefined;
    return (d?.delta ?? d?.text ?? d?.data?.delta ?? d?.data?.text ?? ev.summary ?? "");
  };

  for (const ev of events) {
    const fnode = flowEventNode(ev);

    // Streaming deltas: merge into the trailing row if the kind matches.
    let kind: PipelineRow["kind"] | null = null;
    if (ev.type === "thinking") kind = "thinking";
    else if (ev.type === "assistant_delta") kind = "assistant";

    if (kind) {
      const t = ts(ev);
      const text = deltaText(ev);
      const last = rows[rows.length - 1];
      if (last && last.kind === kind) {
        last.endMs = t;
        last.durationMs = last.endMs - last.startMs;
        last.chunks += 1;
        last.chars += text.length;
      } else {
        rows.push({
          kind,
          label: kind,
          startMs: t,
          endMs: t,
          durationMs: 0,
          chunks: 1,
          chars: text.length,
        });
      }
      continue;
    }

    // Tool call events. Lumi flow.Log("tool_call") fires twice per phase
    // for each tool (once from the `agent` stream without args, once from
    // `session.tool` with args + source) — collapse those duplicates by
    // merging into the trailing row when the preceding event was the same
    // tool name+phase within 1 second.
    const isTool = ev.type === "tool_call" || fnode === "tool_call";
    if (isTool) {
      const d = ev.detail as Record<string, any> | undefined;
      const phase = d?.data?.phase ?? d?.phase ?? "";
      const toolName =
        d?.data?.name ?? d?.data?.tool
        ?? d?.name ?? d?.tool
        ?? "tool";
      const t = ts(ev);
      if (phase === "start" || phase === "") {
        const argsObj = d?.data?.args ?? d?.args;
        let argsSummary = "";
        if (argsObj) {
          try {
            const parsed = typeof argsObj === "string" ? JSON.parse(argsObj) : argsObj;
            argsSummary = parsed?.command ?? JSON.stringify(parsed);
          } catch { argsSummary = String(argsObj); }
        }
        // Deduplicate the agent-stream + session.tool double-emit: if the
        // last row is a `tool · <same name>` start within 1 second, fold
        // this event into it (prefer the variant that carries args).
        const last = rows[rows.length - 1];
        if (last && last.kind === "tool" && last.label === `tool · ${toolName}` && (t - last.startMs) < 1000 && last.durationMs === 0) {
          if (argsSummary && !last.detail) last.detail = argsSummary;
          // keep last.startMs (earliest); update endMs if newer
          if (t > last.endMs) last.endMs = t;
          continue;
        }
        rows.push({
          kind: "tool",
          label: `tool · ${toolName}`,
          detail: argsSummary || undefined,
          startMs: t, endMs: t, durationMs: 0, chunks: 1, chars: 0,
        });
      } else if (phase === "result" || phase === "end") {
        // Attach duration to the most recent tool row of the same name.
        for (let i = rows.length - 1; i >= 0; i--) {
          const r = rows[i];
          if (r.kind === "tool" && r.label === `tool · ${toolName}`) {
            r.endMs = t;
            r.durationMs = r.endMs - r.startMs;
            break;
          }
        }
      }
      continue;
    }

    // Lifecycle markers — show start/end as one-shot rows so the pipeline
    // boundaries are explicit even if no deltas arrived in between.
    if (fnode === "lifecycle_start" || fnode === "lifecycle_end") {
      const t = ts(ev);
      rows.push({
        kind: fnode === "lifecycle_start" ? "lifecycle_start" : "lifecycle_end",
        label: fnode.replace("_", ":"),
        startMs: t, endMs: t, durationMs: 0, chunks: 1, chars: 0,
      });
      continue;
    }

    // Persisted stream summaries (JSONL projection of monitorBus deltas).
    // Raw thinking/assistant_delta events only live in monitorBus (RAM), so
    // for past turns reloaded from JSONL the pipeline rect would otherwise
    // show no streaming rows. first_token opens a row; last_token closes it
    // with chunks/chars from the backend accumulator. If live deltas later
    // arrive (frontend subscribes to monitorBus), the trailing row will
    // already exist and last_token just updates its stats.
    if (fnode === "agent_first_token" || fnode === "thinking_first_token") {
      const t = ts(ev);
      const k: PipelineRow["kind"] = fnode === "thinking_first_token" ? "thinking" : "assistant";
      const last = rows[rows.length - 1];
      if (!last || last.kind !== k || last.durationMs > 0) {
        rows.push({
          kind: k, label: k,
          startMs: t, endMs: t, durationMs: 0, chunks: 0, chars: 0,
        });
      }
      continue;
    }
    if (fnode === "agent_last_token" || fnode === "thinking_last_token") {
      const t = ts(ev);
      const k: PipelineRow["kind"] = fnode === "thinking_last_token" ? "thinking" : "assistant";
      const d = ev.detail as Record<string, any> | undefined;
      const data = (d?.data ?? d) as Record<string, any> | undefined;
      const chunks = Number(data?.chunks ?? 0);
      const chars = Number(data?.chars ?? 0);
      for (let i = rows.length - 1; i >= 0; i--) {
        const r = rows[i];
        if (r.kind === k) {
          r.endMs = t;
          r.durationMs = r.endMs - r.startMs;
          if (chunks > r.chunks) r.chunks = chunks;
          if (chars > r.chars) r.chars = chars;
          break;
        }
      }
      continue;
    }

    // Operational streams (rare, but worth surfacing in the pipeline).
    if (fnode === "compaction" || ev.type === "compaction") {
      rows.push({ kind: "compaction", label: "compaction", startMs: ts(ev), endMs: ts(ev), durationMs: 0, chunks: 1, chars: 0 });
      continue;
    }
    if (fnode === "error" || fnode === "agent_error") {
      const errMsg = ((ev.detail as Record<string, any> | undefined)?.error
        ?? (ev.detail as Record<string, any> | undefined)?.data?.error ?? "") as string;
      rows.push({ kind: "error", label: "error", detail: errMsg ? String(errMsg).slice(0, 120) : undefined, startMs: ts(ev), endMs: ts(ev), durationMs: 0, chunks: 1, chars: 0 });
      continue;
    }
  }
  return rows;
}

// Derive active stage from most recent relevant events
export function deriveActiveStage(events: DisplayEvent[]): ActiveFlowStage {
  const recent = events.slice(-30);
  for (let i = recent.length - 1; i >= 0; i--) {
    const ev = recent[i];
    const key = ev.type === "flow_event" && ev.detail?.node
      ? `flow_event:${ev.detail.node}`
      : ev.type === "flow_enter" && ev.detail?.node
      ? `flow_enter:${ev.detail.node}`
      : ev.type === "flow_exit" && ev.detail?.node
      ? `flow_exit:${ev.detail.node}`
      : ev.type;
    for (const node of [...FLOW_NODES].reverse()) {
      if (node.triggers.includes(key)) return node.id;
    }
  }
  return "idle";
}

export function extractEventRunId(ev: DisplayEvent): string | undefined {
  if (ev.runId) return ev.runId;
  const detail = ev.detail as Record<string, any> | undefined;
  return detail?.run_id ?? detail?.runId ?? detail?.data?.run_id ?? detail?.data?.runId;
}

export function parseChannelSummary(summary: string): string {
  // Match any channel prefix: [telegram], [discord], [slack], [channel], [telegram:sender], etc.
  const m = summary.match(/^\[[^\]]+\]\s*(.*)/);
  if (!m) return summary.trim();
  return (m[1] ?? "").trim();
}

export function turnHasOutput(turn: Turn): boolean {
  return turn.events.some((ev) =>
    ev.type === "tts" ||
    ev.type === "intent_match" ||
    (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed" || ev.detail?.node === "intent_match")),
  );
}

export function turnHasRealChannelInput(turn: Turn): boolean {
  return turn.events.some((ev) => {
    if (ev.type !== "chat_input") return false;
    const msg = parseChannelSummary(ev.summary);
    return msg.length > 0;
  });
}

export function turnHasChatInputEvent(turn: Turn): boolean {
  return turn.events.some((ev) =>
    ev.type === "chat_input" ||
    (ev.type === "flow_event" && ev.detail?.node === "chat_input") ||
    (ev.type === "flow_enter" && ev.detail?.node === "chat_input") ||
    (ev.type === "flow_exit" && ev.detail?.node === "chat_input"),
  );
}

export function turnHasSensingInput(turn: Turn): boolean {
  return turn.events.some((ev) =>
    ev.type === "sensing_input" ||
    (ev.type === "flow_enter" && ev.detail?.node === "sensing_input"),
  );
}

export function turnHasVoicePipeline(turn: Turn): boolean {
  return turn.events.some((ev) =>
    (ev.type === "flow_event" || ev.type === "flow_enter") && ev.detail?.node === "voice_pipeline_start",
  );
}

/** Bracket label from "[voice] hello" / "[motion] ..." on sensing_input / flow_enter sensing_input. */
export function sensingInputBracketType(ev: DisplayEvent): string | null {
  if (ev.type !== "sensing_input" && !(ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
    return null;
  }
  const sensingType = extractSensingType(ev.summary);
  if (sensingType) return sensingType;
  const m = ev.summary.match(/^\[([^\]]+)\]/);
  return m ? m[1] : null;
}

/**
 * Same run_id can include motion (camera) then voice in one session; merge keeps the first segment's type (often "motion").
 * For the turn badge, prefer voice / voice_command when any utterance is present — that is the user's intent.
 */
export function refineTurnTypeFromSensingInputs(turn: Turn): void {
  if (turn.type.startsWith("ambient:") || turn.type === "schedule") {
    return;
  }

  // Reclassify channel turns that are actually sensing events routed via OpenClaw channel.
  // node-host is Lumi's own WebSocket identity in OpenClaw — it sends sensing events AND
  // voice commands via chat.send, so sender=node-host alone doesn't mean "system".
  if (isChannelType(turn.type)) {
    let hasRealUser = false;
    let sensingType: string | null = null;
    let hasSystemMsg = false;
    for (const ev of turn.events) {
      if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
        const d = ev.detail as Record<string, any> | undefined;
        const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
        const sender = d?.sender ?? d?.data?.sender ?? "";
        if (sender && sender !== "node-host") hasRealUser = true;
        const sensType = extractSensingType(msg);
        if (sensType && !sensingType) sensingType = sensType;
        if (sensType || /you just woke up/i.test(msg)) hasSystemMsg = true;
      }
    }
    if (hasRealUser) return; // keep as channel type
    if (sensingType) { turn.type = sensingType; return; }
    // Cron-fired turns: primary signal is the cron_fire flow event emitted by
    // Lumi at lifecycle_start when it correlates an OpenClaw event:"cron"
    // (action:"started"). Fallback to the systemEvent wrapper string match if
    // the event was dropped (OpenClaw broadcasts cron with dropIfSlow:true).
    let isCron = false;
    let cronLabel = "cron";
    for (const ev of turn.events) {
      if (ev.type === "cron_fire" || (ev.type === "flow_event" && ev.detail?.node === "cron_fire")) {
        isCron = true;
        break;
      }
    }
    for (const ev of turn.events) {
      if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
        const d = ev.detail as Record<string, any> | undefined;
        const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
        const sender = d?.sender ?? d?.data?.sender ?? "";
        // Fallback signal + sub-label parsed from text.
        if (!isCron && (!sender || sender === "") && /scheduled reminder/i.test(msg)) {
          isCron = true;
        }
        if (isCron) {
          if (/music/i.test(msg)) cronLabel = "cron:music";
          break;
        }
      }
    }
    if (isCron) { turn.type = cronLabel; return; }
    if (hasSystemMsg) { turn.type = "system"; return; }
    // node-host but normal message (e.g. voice command relayed via chat.send) — keep as channel type
    return;
  }

  // web_chat / voice_command / voice are first-class types from the handler.
  // web_chat wins (/chat UI is the most specific origin) → voice_command → voice.
  let sawWebChat = false;
  let sawVoice = false;
  let sawVoiceCommand = false;
  for (const ev of turn.events) {
    const t = sensingInputBracketType(ev);
    if (t === "web_chat") sawWebChat = true;
    else if (t === "voice_command") sawVoiceCommand = true;
    else if (t === "voice") sawVoice = true;
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const dtype = d?.data?.type ?? d?.type ?? "";
      if (dtype === "web_chat") sawWebChat = true;
      else if (dtype === "voice_command") sawVoiceCommand = true;
      else if (dtype === "voice") sawVoice = true;
    }
  }
  if (sawWebChat) turn.type = "web_chat";
  else if (sawVoiceCommand) turn.type = "voice_command";
  else if (sawVoice) turn.type = "voice";
}

export function groupIntoTurns(events: DisplayEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | null = null;

  function isTurnStart(ev: DisplayEvent): { type: string; path: Turn["path"]; forceNewTurn?: boolean; boundary?: Turn["boundary"] } | null {
    if (ev.type === "sensing_drop") {
      const m = ev.summary.match(/^\[([^\]]+)\]/);
      const t = m ? m[1] : "unknown";
      return { type: t, path: "dropped", forceNewTurn: true };
    }
    if (ev.type === "sensing_queued") {
      const m = ev.summary.match(/^\[([^\]]+)\]/);
      const t = m ? m[1] : "unknown";
      return { type: t, path: "queued", forceNewTurn: true };
    }
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const t = sensingInputBracketType(ev) ?? "unknown";
      return {
        type: t,
        path: "unknown",
        forceNewTurn: t === "voice" || t === "voice_command" || t === "web_chat",
        boundary: t === "voice" || t === "voice_command" ? "mic" : undefined,
      };
    }
    if ((ev.type === "flow_event" || ev.type === "flow_enter") && ev.detail?.node === "voice_pipeline_start") {
      return { type: "voice", path: "unknown", forceNewTurn: true, boundary: "mic" };
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      if (/you just woke up/i.test(msg)) {
        return { type: "system", path: "agent", boundary: "chat" as const };
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      const sender = d?.sender ?? d?.data?.sender ?? "";
      // Skip node-host echo — Lumi's own chat.send echoed back via session.message.
      // These duplicate the sensing_input / voice_pipeline turn that already exists.
      // Detect by: sender is node-host + message contains Lumi-injected directives.
      if (sender === "node-host" && (containsSensingPrefix(msg) || /\[MANDATORY:/.test(msg) || /\[Follow /.test(msg) || /\[REPLY RULE:/.test(msg) || /\[context: current_user=/.test(msg))) {
        return null;
      }
      const sensType = extractSensingType(msg);
      if (sensType) {
        return { type: sensType, path: "agent", boundary: "chat" as const };
      }
      // Extract channel name from summary prefix: [telegram], [discord], [slack], etc.
      const chMatch = ev.summary.match(/^\[([^\]:]+)/);
      return { type: chMatch ? chMatch[1] : "channel", path: "agent", boundary: "chat" };
    }
    const ambientNode = ev.detail?.node ?? "";
    const isAmbientTurn = ev.type === "ambient_action" ||
      ((ev.type === "flow_event" || ev.type === "flow_enter") &&
       ambientNode.startsWith("ambient_") &&
       ambientNode !== "ambient_pause" && ambientNode !== "ambient_resume");
    if (isAmbientTurn) {
      const sub = ambientNode.replace("ambient_", "") || "idle";
      return { type: `ambient:${sub}`, path: "local" };
    }
    if (ev.type === "schedule_trigger" || ev.type === "cron_fire" ||
        (ev.type === "flow_event" && (ev.detail?.node === "schedule_trigger" || ev.detail?.node === "cron_fire"))) {
      return { type: "schedule", path: "agent" };
    }
    return null;
  }

  for (const ev of events) {
    const evRunId = extractEventRunId(ev);
    const start = isTurnStart(ev);
    if (start) {
      const shouldForceSplit = Boolean(start.forceNewTurn);
      // Split if current turn is already done — don't append new turn's events to a finished turn.
      const currentDone = current?.status === "done" || current?.status === "error";
      if (!shouldForceSplit && !currentDone && current && current.runId && evRunId && current.runId === evRunId) {
        current.events.push(ev);
        // Channel chat_input fires twice: first as a placeholder ({run_id,source}
        // only, summary "[chat]") before chat.history resolves the real msg/sender,
        // then again with the real content. The placeholder pins turn.type="chat"
        // and refineTurnTypeFromSensingInputs ignores it (not in CHANNEL_TYPES),
        // so upgrade here when isTurnStart has now resolved a specific type.
        if (current.type === "chat" || current.type === "unknown") {
          current.type = start.type;
        }
        continue;
      }
      if (current) turns.push(current);
      // If another turn already claimed this runId, suffix with seq to keep IDs unique.
      // This prevents duplicate-id bugs in selection (click turn A, turn B stays highlighted).
      let turnId = evRunId || `turn-${ev._seq}`;
      if (evRunId && turns.some((t) => t.id === evRunId)) {
        turnId = `${evRunId}:${ev._seq}`;
      }
      const isTerminalQueued = start.path === "queued";
      const isTerminalDropped = start.path === "dropped";
      const queuedForMs = (() => {
        const d = ev.detail as Record<string, any> | undefined;
        const v = d?.data?.queued_for_ms ?? d?.queued_for_ms;
        return typeof v === "number" ? v : undefined;
      })();
      current = {
        id: turnId,
        runId: evRunId,
        startTime: ev.time,
        endTime: (isTerminalDropped || isTerminalQueued) ? ev.time : undefined,
        type: start.type,
        path: start.path,
        boundary: start.boundary,
        boundaryInstanceSeq: start.boundary ? ev._seq : undefined,
        status: (isTerminalDropped || isTerminalQueued) ? "done" : "active",
        events: [ev],
        queuedForMs,
      };
      continue;
    }

    if (current && current.runId && evRunId && current.runId !== evRunId) {
      const inferredType: Turn["type"] = current.type !== "unknown" ? current.type : "agent";
      const inferredPath: Turn["path"] = current.path !== "unknown" ? current.path : "agent";
      turns.push(current);
      current = {
        id: evRunId,
        runId: evRunId,
        startTime: ev.time,
        type: inferredType,
        path: inferredPath,
        status: "active",
        events: [ev],
      };
      continue;
    }

    if (!current) {
      continue;
    }

    // Split turn when a new lifecycle_start arrives after the turn already saw a lifecycle_end.
    // This handles multiple OpenClaw agent turns mapped to the same device run_id
    // (e.g. sensing + telegram arriving close together while trace is still active).
    const isLifecycleStart = (ev.type === "lifecycle" && ev.phase === "start") ||
      (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start");
    const hasLifecycleEnd = current.events.some((e) =>
      (e.type === "lifecycle" && e.phase === "end") ||
      (e.type === "flow_event" && e.detail?.node === "lifecycle_end"));
    if (isLifecycleStart && hasLifecycleEnd) {
      turns.push(current);
      current = {
        id: evRunId || `turn-${ev._seq}`,
        runId: evRunId || current.runId,
        startTime: ev.time,
        type: "unknown",
        path: "agent",
        status: "active",
        events: [ev],
      };
      continue;
    }

    current.events.push(ev);
    // Capture queued_for_ms when a sensing_input replay event lands inside the turn
    if (current.queuedForMs === undefined &&
        (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input"))) {
      const d = ev.detail as Record<string, any> | undefined;
      const v = d?.data?.queued_for_ms ?? d?.queued_for_ms;
      if (typeof v === "number") current.queuedForMs = v;
    }
    // Classify unknown turns from chat_input events
    if (current.type === "unknown" && (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input"))) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.message ?? d?.data?.message ?? ev.summary ?? "";
      const sensType = extractSensingType(msg);
      if (sensType) {
        current.type = sensType;
      } else {
        const chM = ev.summary.match(/^\[([^\]:]+)/);
        current.type = chM ? chM[1] : "channel";
      }
    }
    if (!current.runId && evRunId) {
      current.runId = evRunId;
      current.id = evRunId;
    }
    // Re-check type on every event so sensing-via-channel turns reclassify immediately
    refineTurnTypeFromSensingInputs(current);

    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      current.path = "local";
    } else if (current.path !== "local") {
      const belongsToTurn = !current.runId || !evRunId || evRunId === current.runId;
      if (belongsToTurn && (evRunId || ev.type === "lifecycle" || ev.type === "thinking")) {
        current.path = "agent";
      }
    }

    if ((ev.type === "lifecycle" && (ev.phase === "end" || ev.phase === "error")) ||
        (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end")) {
      current.status = (ev.phase === "error" || ev.error) ? "error" : "done";
      current.endTime = ev.time;
    }
    if (ev.type === "intent_match") {
      current.status = "done";
      current.endTime = ev.time;
    }
    if (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed" || ev.detail?.node === "no_reply")) {
      current.status = "done";
      current.endTime = ev.time;
    }
    // chat_final_empty: OpenClaw sent state:"final" with empty Message for a
    // Lumi-format runId that never opened a lifecycle. Factual close event —
    // no interpretation. (Legacy `turn_steered` is back-compat for old JSONL.)
    // chat_final_ok: same shape but non-empty Message — slash commands
    // (/status, /new, /compact) dispatched pre-LLM by OpenClaw return a
    // payload without ever opening a lifecycle.
    if (ev.type === "flow_event" && (ev.detail?.node === "chat_final_empty" || ev.detail?.node === "chat_final_ok" || ev.detail?.node === "turn_steered")) {
      current.status = "done";
      current.endTime = ev.time;
    }
    if (current.type.startsWith("ambient:") && ev.type === "flow_exit" && ev.detail?.node?.startsWith("ambient_")) {
      current.status = "done";
      current.endTime = ev.time;
    }
  }
  if (current) turns.push(current);

  // Merge fragmented segments that share the same run_id
  const merged: Turn[] = [];
  const runIndex = new Map<string, number>();
  for (const turn of turns) {
    if (!turn.runId) {
      merged.push(turn);
      continue;
    }
    const idx = runIndex.get(turn.runId);
    if (idx === undefined) {
      runIndex.set(turn.runId, merged.length);
      merged.push(turn);
      continue;
    }
    if (turn.boundaryInstanceSeq !== undefined) {
      merged.push(turn);
      runIndex.set(turn.runId, merged.length - 1);
      continue;
    }

    const base = merged[idx];
    base.events.push(...turn.events);
    if (base.status !== "error" && turn.status === "error") base.status = "error";
    else if (base.status === "active" && turn.status === "done") base.status = "done";
    // chat_final_empty may arrive in a later fragment (events of an interleaving
    // turn split chat-N's events), so promote "active → done" here too.
    // The fragment may not carry status="done" itself (when it was created
    // via the runId-switch branch the status-update block is skipped), so also
    // scan its raw events for the chat_final_empty (or legacy turn_steered) marker.
    else if (base.status === "active" && turn.events.some(
      (e) => e.type === "flow_event" && (e.detail?.node === "chat_final_empty" || e.detail?.node === "turn_steered"))) {
      base.status = "done";
      if (!base.endTime) base.endTime = turn.endTime || turn.startTime;
    }
    if (!base.endTime && turn.endTime) base.endTime = turn.endTime;
    else if (base.endTime && turn.endTime && turn.endTime > base.endTime) base.endTime = turn.endTime;
    if (base.path !== "agent" && turn.path === "agent") base.path = "agent";
    if (base.type === "unknown" && turn.type !== "unknown") base.type = turn.type;
  }
  for (const turn of merged) {
    turn.events.sort((a, b) => a._seq - b._seq);
  }

  // Merge adjacent Telegram fallback + agent output fragments
  const stitched: Turn[] = [];
  for (const turn of merged) {
    const prev = stitched[stitched.length - 1];
    if (!prev) {
      stitched.push(turn);
      continue;
    }
    const prevHasNoOutput = !turnHasOutput(prev);
    const currLooksAgentReply = turn.path === "agent" && turnHasOutput(turn);
    const prevTs = new Date(prev.endTime || prev.startTime).getTime();
    const currTs = new Date(turn.startTime).getTime();
    const closeInTime = Number.isFinite(prevTs) && Number.isFinite(currTs) && (currTs - prevTs) <= 30_000;

    const prevIsChannelFallback = isChannelType(prev.type) && !turnHasRealChannelInput(prev);
    if (prevIsChannelFallback && prevHasNoOutput && currLooksAgentReply && closeInTime) {
      if (turn.runId && /^lumi-(chat|sensing)-/i.test(turn.runId)) {
        stitched.push(turn);
        continue;
      }
      prev.events.push(...turn.events);
      prev.events.sort((a, b) => a._seq - b._seq);
      prev.status = turn.status === "error" ? "error" : turn.status;
      prev.endTime = turn.endTime || prev.endTime;
      prev.path = "agent";
      continue;
    }

    const prevIsSensingNoOutput = turnHasSensingInput(prev) && prevHasNoOutput;
    const currIsOrphanOutput = !turnHasSensingInput(turn) && !turnHasRealChannelInput(turn) && turnHasOutput(turn);
    if (prevIsSensingNoOutput && currIsOrphanOutput && closeInTime) {
      prev.events.push(...turn.events);
      prev.events.sort((a, b) => a._seq - b._seq);
      prev.status = turn.status === "error" ? "error" : turn.status;
      prev.endTime = turn.endTime || prev.endTime;
      prev.path = "agent";
      continue;
    }

    stitched.push(turn);
  }

  for (const turn of stitched) {
    refineTurnTypeFromSensingInputs(turn);
    if (isChannelType(turn.type) && (!turnHasChatInputEvent(turn))) {
      turn.type = "unknown";
    }
    // chat_input without resolved message is still a channel turn — keep it.
    // Done turn with no recognizable input source → unknown,
    // but only if the type is still generic (channel/unknown). Preserve
    // specific types that were already resolved from sensing data
    // (e.g. voice, motion, presence.enter) arriving via chat_send.
    if (turn.status === "done" && !turnHasSensingInput(turn) && !turnHasRealChannelInput(turn) && !turnHasVoicePipeline(turn)) {
      if (isChannelType(turn.type) || turn.type === "unknown") {
        turn.type = "unknown";
      }
    }
  }

  // Detect session breaks
  for (let i = 1; i < stitched.length; i++) {
    const prev = stitched[i - 1];
    const curr = stitched[i];
    const prevEnd = new Date(prev.endTime || prev.startTime).getTime();
    const currStart = new Date(curr.startTime).getTime();
    if (currStart - prevEnd > 60_000) {
      curr.sessionBreak = true;
    }
  }

  return stitched.reverse();
}

// Extract runtime info for each node from turn events
export function extractNodeInfo(events: DisplayEvent[]): NodeInfoMap {
  const info: NodeInfoMap = {
    mic_input: [], cam_input: [], button_input: [], channel_input: [], webchat_input: [], intent_check: [], local_match: [],
    agent_call: [], agent_thinking: [], tool_exec: [],
    agent_response: [], tts_speak: [], schedule_trigger: [],
    lumi_gate: [], hw_led: [], hw_servo: [], hw_emotion: [], hw_audio: [], hw_wellbeing: [], hw_mood: [], hw_music_suggestion: [], hw_posture: [], tg_out: [], tg_alert: [],
    ambient: [],
  };
  const fmtToken = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  const pushUnique = (arr: string[], line: string) => {
    if (!line) return;
    if (!arr.includes(line)) arr.push(line);
  };
  const pushAgentResponse = (line: string) => pushUnique(info.agent_response, line);
  const pushLLMTokens = (line: string) => {
    pushUnique(info.agent_call, line);
    pushUnique(info.agent_thinking, line);
    pushUnique(info.agent_response, line);
  };

  // Sensing → lifecycle timing (used for agent_call node info below)
  let sensingEnterTs = 0;
  let lifecycleStartTs = 0;
  for (const ev of events) {
    const ts = new Date(ev.time).getTime();
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      if (!sensingEnterTs) sensingEnterTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start") {
      if (!lifecycleStartTs) lifecycleStartTs = ts;
    }
  }
  for (const ev of events) {
    if (ev.type === "sensing_input") {
      const m = ev.summary.match(/^\[([^\]]+)\]\s*(.*)/);
      const sType = m?.[1] ?? "";
      const d = ev.detail as Record<string, any> | undefined;
      const dtype = d?.type ?? d?.data?.type ?? "";
      const isWeb = sType === "web_chat" || dtype === "web_chat";
      if (isWeb) {
        info.webchat_input.push(`"${m?.[2] ?? ev.summary}"`);
      } else {
        const isButton = /^touch\./i.test(sType);
        const isCam = !isButton && /motion|presence|light/i.test(sType);
        const target = isButton ? info.button_input : isCam ? info.cam_input : info.mic_input;
        if (m) {
          target.push(`type: ${m[1]}`, `"${m[2]}"`);
        } else {
          target.push(ev.summary);
        }
      }
    }
    {
      const aNode = ev.detail?.node ?? "";
      const isAmbientInfo = ev.type === "ambient_action" ||
        ((ev.type === "flow_event" || ev.type === "flow_enter" || ev.type === "flow_exit") &&
         aNode.startsWith("ambient_") && aNode !== "ambient_pause" && aNode !== "ambient_resume");
      if (isAmbientInfo) {
        const sub = aNode.replace("ambient_", "") || ev.summary || "";
        if (info.ambient.length < 3) info.ambient.push(`${sub}: ${ev.summary || "active"}`);
      }
    }
    if (ev.type === "schedule_trigger" || ev.type === "cron_fire" ||
        (ev.type === "flow_event" && (ev.detail?.node === "schedule_trigger" || ev.detail?.node === "cron_fire"))) {
      const d = ev.detail as Record<string, string> | undefined;
      info.schedule_trigger.push(d?.name ?? ev.summary ?? "cron fired");
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const msg = parseChannelSummary(ev.summary);
      info.channel_input.push(`"${msg || CHANNEL_FALLBACK_MESSAGE}"`);
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.data?.message ?? d?.message ?? "";
      const tts = d?.data?.tts ?? d?.tts ?? "";
      const rule = d?.data?.rule ?? d?.rule ?? "";
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      info.intent_check.push("⚡ local match");
      const parts = [`"${msg}" → ${tts}`];
      if (rule) parts.push(`rule: ${rule}`);
      for (const a of actions) {
        // Convert "POST /path {body}" to full curl command
        const m = a.match(/^(POST|GET|PUT|DELETE)\s+(\/\S+)\s*(.*)?$/);
        if (m) {
          const [, method, path, body] = m;
          let curl = `curl -s -X ${method} http://127.0.0.1:5001${path}`;
          if (body) curl += ` -H "Content-Type: application/json" -d '${body}'`;
          parts.push(`🔧 ${curl}`);
        } else {
          parts.push(`🔧 ${a}`);
        }
      }
      info.local_match.push(msg ? parts.join("\n") : ev.summary);
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      info.intent_check.push("→ agent route");
      const d = ev.detail as Record<string, any> | undefined;
      const hasImage = d?.data?.has_image || d?.has_image;
      const imgBytes = Number(d?.data?.image_bytes ?? d?.image_bytes ?? 0);
      const chatMsg = d?.data?.message ?? d?.message ?? "";
      if (hasImage) info.agent_call.push(`📷 image attached (~${Math.round(imgBytes * 3 / 4 / 1024)}KB)`);
      if (chatMsg) {
        // Extract all [snapshot: /path] entries
        const snapAllRe = /\[snapshot:\s*([^\]]+)\]/g;
        let snapM;
        while ((snapM = snapAllRe.exec(chatMsg)) !== null) {
          info.agent_call.push(`🖼 snapshot: ${snapM[1].trim()}`);
        }
        // Replace any earlier 📩 from sensing_input with the exact text sent to OpenClaw.
        // Show the chat_send message VERBATIM — no strip — so the user can visually verify
        // whether the backend stripped [snapshot: ...] before sending to the LLM.
        const idx = info.agent_call.findIndex((l) => l.startsWith("📩"));
        if (idx >= 0) info.agent_call[idx] = `📩 ${chatMsg}`;
        else info.agent_call.push(`📩 ${chatMsg}`);
      }
    }
    // Show input message on agent_call node (fallback if chat_send hasn't fired yet).
    // Backend strips [snapshot:...] from chat_send text; sensing_input retains the full
    // text so snapshots (🖼 lines) come from here.
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.data?.message ?? d?.message ?? ev.summary ?? "";
      if (msg) {
        const snapAllRe = /\[snapshot:\s*([^\]]+)\]/g;
        let snapM;
        while ((snapM = snapAllRe.exec(msg)) !== null) {
          const line = `🖼 snapshot: ${snapM[1].trim()}`;
          if (!info.agent_call.includes(line)) info.agent_call.push(line);
        }
        if (!info.agent_call.some((l) => l.startsWith("📩"))) {
          info.agent_call.push(`📩 ${msg}`);
        }
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const msg = d?.message ?? d?.data?.message ?? "";
      const sender = d?.sender ?? d?.data?.sender ?? "";
      if (msg && !info.agent_call.some((l) => l.startsWith("📩"))) {
        info.agent_call.push(`📩 ${sender ? `[${sender}] ` : ""}${msg}`);
      }
    }
    if (ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call")) {
      const d = ev.detail as Record<string, any> | undefined;
      const phase = d?.phase ?? d?.data?.phase ?? "";
      // Only show tool start (has args), skip update/result phases
      if (phase !== "start" && phase !== "") continue;
      const rawArgs = d?.args ?? d?.data?.args ?? "";
      let argsSummary = "";
      if (rawArgs) {
        try {
          const parsed = typeof rawArgs === "string" ? JSON.parse(rawArgs) : rawArgs;
          if (parsed?.command) {
            argsSummary = (parsed.command as string);
          } else {
            argsSummary = JSON.stringify(parsed);
          }
        } catch { argsSummary = String(rawArgs); }
      }
      if (argsSummary) {
        const entry = `🔧 ${argsSummary}`;
        if (!info.tool_exec.includes(entry)) info.tool_exec.push(entry);
        // Also surface emotion/led/servo tool calls in their HW nodes with LLM source label
        if (/\/emotion/.test(argsSummary)) pushUnique(info.hw_emotion, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/led|\/scene/.test(argsSummary)) pushUnique(info.hw_led, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/servo/.test(argsSummary)) pushUnique(info.hw_servo, `🤖 LLM tool → ${argsSummary}`);
        else if (/\/audio/.test(argsSummary)) pushUnique(info.hw_audio, `🤖 LLM tool → ${argsSummary}`);
      }
    }
    if (ev.type === "thinking" || (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start")) {
      if (ev.type === "thinking" && ev.summary) {
        info.agent_thinking.push(`"${ev.summary}…"`);
      }
      if (ev.type === "flow_event" && info.agent_thinking.length === 0) {
        info.agent_thinking.push("reasoning…");
      }
    }
    // Thinking from chat.history (fallback when streaming too fast)
    if (ev.type === "flow_event" && ev.detail?.node === "agent_thinking") {
      const d = ev.detail as Record<string, any> | undefined;
      const text = d?.data?.text ?? d?.text ?? "";
      if (text && !info.agent_thinking.some((l) => l.startsWith("🧠"))) {
        info.agent_thinking.push(`🧠 ${text}`);
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      pushAgentResponse("🚫 [no reply] — agent decided to do nothing");
    }
    if (ev.type === "chat_response" || (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end")) {
      const d = ev.detail as Record<string, any> | undefined;
      if (d?.message && !info.agent_response.some((l) => l.startsWith('"'))) {
        info.agent_response.push(`"${d.message}"`);
      }
      const dataErr = d?.data?.error;
      if (dataErr && info.agent_response.length < 2) {
        info.agent_response.push(`❌ ${dataErr}`);
      }
    }
    if (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed"))) {
      const d = ev.detail as Record<string, any> | undefined;
      const text = d?.data?.text ?? d?.text ?? "";
      const isSuppressed = ev.type === "flow_event" && d?.node === "tts_suppressed";
      const label = isSuppressed ? "💬" : "🔊";
      if (text && info.tts_speak.length < 2) {
        info.tts_speak.push(`${label} "${text}"`);
      }
      if (text && !info.agent_response.some((l) => l.startsWith('"'))) {
        info.agent_response.push(`"${text}"`);
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "telegram_alert_broadcast") {
      const d = ev.detail as Record<string, any> | undefined;
      const sessions = Number(d?.data?.sessions ?? 0);
      const msg = d?.data?.message ?? "";
      if (sessions) info.tg_alert.push(`📢 broadcast → ${sessions} session${sessions > 1 ? "s" : ""}`);
      if (msg) {
        const short = msg.length > 80 ? msg.slice(0, 80) + "…" : msg;
        info.tg_alert.push(`💬 ${short}`);
      }
    }
    if (ev.type === "lifecycle") {
      if (ev.phase === "start") info.agent_call.push(`run: ${ev.runId ?? "?"}`);
      if (ev.phase === "error") {
        pushAgentResponse(`❌ ${ev.error ?? ev.summary ?? "error"}`);
      }
      if (ev.phase === "end") {
        pushAgentResponse(ev.error ? `❌ ${ev.error}` : "✓ done");
        const d = ev.detail as Record<string, string> | undefined;
        if (d?.inputTokens) {
          const inp = parseInt(d.inputTokens, 10);
          const out = parseInt(d.outputTokens ?? "0", 10);
          pushLLMTokens(`tokens: ${fmtToken(inp)} in / ${fmtToken(out)} out`);
        }
      }
    }
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const d = ev.detail as Record<string, any> | undefined;
      const u = d?.data;
      const inTok = Number(u?.input_tokens ?? 0);
      const outTok = Number(u?.output_tokens ?? 0);
      const cacheRead = Number(u?.cache_read_tokens ?? 0);
      const cacheWrite = Number(u?.cache_write_tokens ?? 0);
      const total = Number(u?.total_tokens ?? 0);
      if (inTok || outTok) pushLLMTokens(`tokens: ${fmtToken(inTok)} in / ${fmtToken(outTok)} out`);
      if (cacheRead || cacheWrite) pushLLMTokens(`cache: ${fmtToken(cacheRead)} read / ${fmtToken(cacheWrite)} write`);
      if (total) pushLLMTokens(`total: ${fmtToken(total)}`);
      // Effective (billed) tokens: cache read costs 10% of input price
      const billed = inTok + cacheWrite + Math.round(cacheRead * 0.1) + outTok;
      if (billed) pushLLMTokens(`billed: ~${fmtToken(billed)}`);
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end") {
      const d = ev.detail as Record<string, any> | undefined;
      const err = d?.data?.error;
      if (err) info.agent_response.push(`❌ ${err}`);
    }
    if (ev.type === "hw_call" || (ev.type === "flow_event" && ev.detail?.node === "hw_call")) {
      const d = ev.detail as Record<string, any> | undefined;
      const path = d?.data?.path ?? d?.path ?? "";
      const args = d?.data?.args ?? d?.args ?? "";
      if (path) pushUnique(info.tool_exec, `⚙ HW ${path} ${args}`);
    }
    if (ev.type === "hw_only_reply" || (ev.type === "flow_event" && ev.detail?.node === "hw_only_reply")) {
      pushAgentResponse("⚙ HW-only reply (no spoken text)");
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      const d = ev.detail as Record<string, any> | undefined;
      const tts = d?.data?.tts ?? d?.tts ?? "";
      if (tts && info.tts_speak.length < 3) info.tts_speak.push(`💡 ${tts}`);
    }
    // HW marker events: extract path+body from either flow_event (detail.data) or direct event (summary = "/path body")
    const parseHWEvent = (ev: DisplayEvent, fallbackPath: string): { path: string; body: string } => {
      const d = ev.detail as Record<string, any> | undefined;
      if (d?.data?.path && d?.data?.args) return { path: d.data.path, body: d.data.args };
      const s = ev.summary ?? "";
      const i = s.indexOf(" ");
      return i > 0 ? { path: s.slice(0, i), body: s.slice(i + 1) } : { path: fallbackPath, body: s };
    };
    if (ev.type === "hw_emotion" || (ev.type === "flow_event" && ev.detail?.node === "hw_emotion")) {
      const { path, body } = parseHWEvent(ev, "/emotion");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_emotion, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -H "Content-Type: application/json" -d '${body}'`);
        const m = body.match(/"emotion"\s*:\s*"([^"]+)"/);
        pushUnique(info.lumi_gate, `🎭 → ${m ? m[1] : "emotion"}`);
      }
    }
    if (ev.type === "hw_led" || (ev.type === "flow_event" && ev.detail?.node === "hw_led")) {
      const { path, body } = parseHWEvent(ev, "/led/solid");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_led, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -d '${body}'`);
        pushUnique(info.lumi_gate, `💡 → LED ${path}`);
      }
    }
    if (ev.type === "hw_servo" || (ev.type === "flow_event" && ev.detail?.node === "hw_servo")) {
      const { path, body } = parseHWEvent(ev, "/servo/play");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_servo, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -d '${body}'`);
        pushUnique(info.lumi_gate, `🤖 → servo ${path}`);
      }
    }
    if (ev.type === "hw_audio" || (ev.type === "flow_event" && ev.detail?.node === "hw_audio")) {
      const { path, body } = parseHWEvent(ev, "/audio/play");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_audio, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5001${path} -d '${body}'`);
        pushUnique(info.lumi_gate, `🎵 → audio ${path}`);
      }
    }
    if (ev.type === "hw_wellbeing" || (ev.type === "flow_event" && ev.detail?.node === "hw_wellbeing")) {
      const { path, body } = parseHWEvent(ev, "/wellbeing/log");
      if (body && body.startsWith("{")) {
        // Wellbeing log goes to Lumi (port 5000), not LeLamp (5001), via the /api/ prefix.
        pushUnique(info.hw_wellbeing, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -d '${body}'`);
        const m = body.match(/"action"\s*:\s*"([^"]+)"/);
        pushUnique(info.lumi_gate, `💧 → wellbeing ${m ? m[1] : path}`);
      }
    }
    if (ev.type === "hw_mood" || (ev.type === "flow_event" && ev.detail?.node === "hw_mood")) {
      const { path, body } = parseHWEvent(ev, "/mood/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_mood, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -d '${body}'`);
        const kindMatch = body.match(/"kind"\s*:\s*"([^"]+)"/);
        const moodMatch = body.match(/"mood"\s*:\s*"([^"]+)"/);
        const kind = kindMatch ? kindMatch[1] : "log";
        const mood = moodMatch ? moodMatch[1] : "?";
        pushUnique(info.lumi_gate, `🧠 → mood ${kind}=${mood}`);
      }
    }
    if (ev.type === "hw_music_suggestion" || (ev.type === "flow_event" && ev.detail?.node === "hw_music_suggestion")) {
      const { path, body } = parseHWEvent(ev, "/music-suggestion/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_music_suggestion, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -d '${body}'`);
        const triggerMatch = body.match(/"trigger"\s*:\s*"([^"]+)"/);
        pushUnique(info.lumi_gate, `🎼 → music-suggest ${triggerMatch ? triggerMatch[1] : path}`);
      }
    }
    if (ev.type === "hw_posture" || (ev.type === "flow_event" && ev.detail?.node === "hw_posture")) {
      const { path, body } = parseHWEvent(ev, "/posture/log");
      if (body && body.startsWith("{")) {
        pushUnique(info.hw_posture, `⚡ HW marker → curl -s -X POST http://127.0.0.1:5000/api${path} -d '${body}'`);
        const kindMatch = body.match(/"kind"\s*:\s*"([^"]+)"/);
        pushUnique(info.lumi_gate, `🪑 → posture ${kindMatch ? kindMatch[1] : path}`);
      }
    }
    if (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed")) {
      pushUnique(info.lumi_gate, "🔊 → TTS");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "tts_suppressed") {
      const d = ev.detail as Record<string, any> | undefined;
      const reason = d?.data?.reason ?? "suppressed";
      pushUnique(info.lumi_gate, `🔇 → TTS suppressed (${reason})`);
    }
    if (ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      pushUnique(info.lumi_gate, "🚫 → no reply");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "hw_only_reply") {
      pushUnique(info.lumi_gate, "⚙ → HW only (no speech)");
    }
    if (ev.type === "flow_event" && ev.detail?.node === "telegram_alert_broadcast") {
      pushUnique(info.lumi_gate, "📢 → broadcast");
    }
  }
  // After processing all events: if lifecycle_end was seen but no response/no_reply, mark silent
  const hasLifecycleEnd = events.some((e) =>
    (e.type === "lifecycle" && e.phase === "end") ||
    (e.type === "flow_event" && e.detail?.node === "lifecycle_end"));
  const hasNoReply = events.some((e) => e.type === "flow_event" && e.detail?.node === "no_reply");
  if (hasLifecycleEnd && info.agent_response.length === 0 && !hasNoReply) {
    info.agent_response.push("💤 no output — processed silently");
  }

  // --- Per-node duration from timestamp deltas ---
  const fmtDur = (ms: number) => ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
    : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;

  let nSensingTs = 0, nChatSendTs = 0, nChatInputTs = 0;
  let nLifecycleStartTs = 0, nLifecycleEndTs = 0, nTtsTs = 0;
  let nLlmFirstTokenTs = 0;
  let nFirstToolTs = 0, nLastToolResultTs = 0;
  let nToolTotalMs = 0, nToolStartTs = 0;
  let nIntentMatchTs = 0;
  let nSensingExitDur = 0; // duration_ms from flow_exit:sensing_input
  let nLastBatchResultTs = 0, nInterToolMs = 0; // inter-tool LLM thinking

  for (const ev of events) {
    const ts = new Date(ev.time).getTime();
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      if (!nSensingTs) nSensingTs = ts;
    }
    if (ev.type === "flow_exit" && ev.detail?.node === "sensing_input") {
      const dataObj = typeof ev.detail?.data === "string" ? (() => { try { return JSON.parse(ev.detail!.data); } catch { return null; } })() : null;
      const dur = Number(ev.detail?.dur_ms ?? dataObj?.dur_ms ?? 0);
      if (dur > 0) nSensingExitDur = dur;
    }
    if (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match")) {
      if (!nIntentMatchTs) nIntentMatchTs = ts;
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      if (!nChatInputTs) nChatInputTs = ts;
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      if (!nChatSendTs) nChatSendTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start") {
      if (!nLifecycleStartTs) nLifecycleStartTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end") {
      nLifecycleEndTs = ts;
    }
    // First thinking or assistant delta = LLM started streaming (warmup edge).
    // Replaces the legacy `llm_first_token` flow event marker with a direct
    // observation of the actual first stream delta.
    {
      const isLiveDelta = ev.type === "thinking" || ev.type === "assistant_delta";
      const isMarker = ev.type === "flow_event"
        && (ev.detail?.node === "agent_first_token" || ev.detail?.node === "thinking_first_token");
      if ((isLiveDelta || isMarker) && !nLlmFirstTokenTs) {
        nLlmFirstTokenTs = ts;
      }
    }
    if (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed"))) {
      if (!nTtsTs) nTtsTs = ts;
    }
    const isToolCall = ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call");
    if (isToolCall) {
      const d = ev.detail as Record<string, any> | undefined;
      const phase = d?.data?.phase ?? d?.phase ?? "";
      if (phase === "start") {
        if (!nFirstToolTs) nFirstToolTs = ts;
        if (nLastBatchResultTs && ts > nLastBatchResultTs) {
          nInterToolMs += ts - nLastBatchResultTs;
          nLastBatchResultTs = 0;
        }
        nToolStartTs = ts;
      }
      if (phase === "result") {
        nLastToolResultTs = ts;
        nLastBatchResultTs = ts;
        if (nToolStartTs) { nToolTotalMs += ts - nToolStartTs; nToolStartTs = 0; }
      }
    }
  }

  // sensing_input exit duration → mic/cam input node
  if (nSensingExitDur > 0) {
    const dur = fmtDur(nSensingExitDur);
    if (info.mic_input.length > 0) info.mic_input.unshift(`⏱ ${dur}`);
    else if (info.cam_input.length > 0) info.cam_input.unshift(`⏱ ${dur}`);
  }

  // intent_check: sensing → chat_send or intent_match (whichever comes first)
  if (nSensingTs || nChatInputTs) {
    const from = nSensingTs || nChatInputTs;
    const to = nIntentMatchTs || nChatSendTs;
    if (to && to > from) {
      const ms = to - from;
      info.intent_check.unshift(`⏱ ${fmtDur(ms)}`);
    }
  }

  // local_match: intent_match duration (instant, but show if > 0)
  // (local_match is triggered by intent_match, timing is included in intent_check)

  // agent_call has no duration of its own — it's the act of Lumi writing
  // chat.send to the WS, which is sub-millisecond on localhost. The 1-2s
  // commonly seen between chat_send and lifecycle_start is OpenClaw's
  // internal init (queue + hooks + skill load + prompt build), shown on
  // the pipeline header summary as "init Xs". Don't add a ⏱ here so the
  // node info doesn't mislabel that time as belonging to agent_call.

  // agent_thinking: post-warmup streaming time. Use first thinking/assistant
  // delta timestamp as start when available (warmup edge — observed directly
  // from the stream, no marker event needed); otherwise fall back to
  // lifecycle_start.
  const thinkStartTs = nLlmFirstTokenTs || nLifecycleStartTs;
  if (thinkStartTs) {
    const to = nFirstToolTs || nLifecycleEndTs;
    if (to && to > thinkStartTs) {
      const streaming = to - thinkStartTs;
      const totalThinking = streaming + nInterToolMs;
      if (nInterToolMs > 0) {
        info.agent_thinking.unshift(`⏱ ${fmtDur(totalThinking)} (first ${fmtDur(streaming)} + between tools ${fmtDur(nInterToolMs)})`);
      } else {
        info.agent_thinking.unshift(`⏱ ${fmtDur(streaming)}`);
      }
    }
  }

  // tool_exec: total tool execution time
  if (nToolTotalMs > 0) {
    info.tool_exec.unshift(`⏱ ${fmtDur(nToolTotalMs)}`);
  }

  // agent_response: last tool_result → lifecycle_end (or lifecycle_start → lifecycle_end if no tools)
  if (nLastToolResultTs && nLifecycleEndTs && nLifecycleEndTs > nLastToolResultTs) {
    const ms = nLifecycleEndTs - nLastToolResultTs;
    if (ms > 0) info.agent_response.unshift(`⏱ ${fmtDur(ms)}`);
  }

  // tts_speak: lifecycle_end → tts_send
  if (nLifecycleEndTs && nTtsTs) {
    const ms = nTtsTs - nLifecycleEndTs;
    if (ms > 0 && ms < 30_000) info.tts_speak.unshift(`⏱ ${fmtDur(ms)}`);
  } else if (nIntentMatchTs && nTtsTs) {
    // Local path: intent_match → tts
    const ms = nTtsTs - nIntentMatchTs;
    if (ms > 0 && ms < 30_000) info.tts_speak.unshift(`⏱ ${fmtDur(ms)}`);
  }

  return info;
}

// Timing breakdown for a turn — displayed as a summary bar above the pipeline
export interface TurnTiming {
  total: number;       // start → end (ms)
  segments: { label: string; ms: number; color: string; from?: string; to?: string }[];
}

export function extractTurnTiming(events: DisplayEvent[], startTime?: string, endTime?: string): TurnTiming | null {
  if (!startTime || !endTime) return null;
  const totalMs = new Date(endTime).getTime() - new Date(startTime).getTime();
  if (!Number.isFinite(totalMs) || totalMs <= 0) return null;

  const fmtDur = (ms: number) => ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
    : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;

  let sensingTs = 0, chatSendTs = 0, chatInputTs = 0;
  let lifecycleStartTs = 0, lifecycleEndTs = 0, ttsTs = 0;
  let llmFirstTokenTs = 0;
  let firstToolCallTs = 0, lastToolResultTs = 0;
  let toolTotalMs = 0, toolStartTs = 0;
  // Track inter-tool LLM thinking: time between a batch of tool results and the next tool start.
  let lastBatchResultTs = 0, interToolMs = 0;

  for (const ev of events) {
    const ts = new Date(ev.time).getTime();
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      if (!sensingTs) sensingTs = ts;
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      if (!chatInputTs) chatInputTs = ts;
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      if (!chatSendTs) chatSendTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_start") {
      if (!lifecycleStartTs) lifecycleStartTs = ts;
    }
    if (ev.type === "flow_event" && ev.detail?.node === "lifecycle_end") {
      lifecycleEndTs = ts;
    }
    // First thinking or assistant delta = LLM started streaming. Sourced
    // from either live monitorBus deltas (type === "thinking" |
    // "assistant_delta") or the persisted JSONL marker flow events
    // (agent_first_token / thinking_first_token), whichever arrives first.
    const isFirstTokenMarker = ev.type === "flow_event"
      && (ev.detail?.node === "agent_first_token" || ev.detail?.node === "thinking_first_token");
    if ((ev.type === "thinking" || ev.type === "assistant_delta" || isFirstTokenMarker) && !llmFirstTokenTs) {
      llmFirstTokenTs = ts;
    }
    if (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed"))) {
      if (!ttsTs) ttsTs = ts;
    }
    const isToolCall = ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call");
    if (isToolCall) {
      const d = ev.detail as Record<string, any> | undefined;
      const phase = d?.data?.phase ?? d?.phase ?? "";
      if (phase === "start") {
        if (!firstToolCallTs) firstToolCallTs = ts;
        // If a previous batch finished, the gap is LLM thinking between rounds.
        if (lastBatchResultTs && ts > lastBatchResultTs) {
          interToolMs += ts - lastBatchResultTs;
          lastBatchResultTs = 0;
        }
        toolStartTs = ts;
      }
      if (phase === "result") {
        lastToolResultTs = ts;
        lastBatchResultTs = ts;
        if (toolStartTs) { toolTotalMs += ts - toolStartTs; toolStartTs = 0; }
      }
    }
  }

  const segments: TurnTiming["segments"] = [];
  // Sensing / input processing — typically <5ms, only show if notable (>50ms)
  if (sensingTs && chatSendTs) {
    const ms = chatSendTs - sensingTs;
    if (ms > 50) segments.push({ label: `lelamp detect ${fmtDur(ms)}`, ms, color: "var(--lm-amber)", from: "sensing_input (lumi)", to: "chat_send (lumi)" });
  }

  // Queue → OpenClaw start
  const callTs = chatSendTs || chatInputTs;
  if (callTs && lifecycleStartTs) {
    const ms = lifecycleStartTs - callTs;
    if (ms > 0) segments.push({ label: `openclaw init ${fmtDur(ms)}`, ms, color: "var(--lm-blue)", from: "chat_send (lumi)", to: "lifecycle_start (openclaw)" });
  }

  // LLM warmup: lifecycle_start → first thinking/assistant delta. The model
  // is reasoning silently before any token streams. Source: direct stream
  // observation (no marker event).
  if (lifecycleStartTs && llmFirstTokenTs && llmFirstTokenTs > lifecycleStartTs) {
    const ms = llmFirstTokenTs - lifecycleStartTs;
    segments.push({ label: `llm warmup ${fmtDur(ms)}`, ms, color: "var(--lm-blue)", from: "lifecycle_start (openclaw)", to: "first delta (openclaw)" });
  }

  // LLM streaming (post-warmup): first delta → first tool_call (or
  // lifecycle_end when no tool fired).
  if (llmFirstTokenTs && firstToolCallTs && firstToolCallTs > llmFirstTokenTs) {
    const ms = firstToolCallTs - llmFirstTokenTs;
    segments.push({ label: `llm streaming ${fmtDur(ms)}`, ms, color: "var(--lm-purple)", from: "first delta (openclaw)", to: "first tool_call (openclaw)" });
  } else if (lifecycleStartTs && firstToolCallTs && !llmFirstTokenTs) {
    // Tool fired but no thinking/assistant delta seen first — silent reasoning
    // straight into a tool call. Attribute the gap as thinking.
    const ms = firstToolCallTs - lifecycleStartTs;
    segments.push({ label: `llm thinking ${fmtDur(ms)}`, ms, color: "var(--lm-purple)", from: "lifecycle_start (openclaw)", to: "first tool_call (openclaw)" });
  } else if (llmFirstTokenTs && lifecycleEndTs && !firstToolCallTs && lifecycleEndTs > llmFirstTokenTs) {
    const ms = lifecycleEndTs - llmFirstTokenTs;
    segments.push({ label: `llm streaming ${fmtDur(ms)}`, ms, color: "var(--lm-purple)", from: "first delta (openclaw)", to: "lifecycle_end (openclaw)" });
  } else if (lifecycleStartTs && lifecycleEndTs && !firstToolCallTs && !llmFirstTokenTs) {
    const ms = lifecycleEndTs - lifecycleStartTs;
    segments.push({ label: `llm processing ${fmtDur(ms)}`, ms, color: "var(--lm-purple)", from: "lifecycle_start (openclaw)", to: "lifecycle_end (openclaw)" });
  }

  // Tool exec
  if (toolTotalMs > 0) {
    segments.push({ label: `tool exec ${fmtDur(toolTotalMs)}`, ms: toolTotalMs, color: "#f59e0b", from: "tool_call start (openclaw)", to: "tool_call result (lumi)" });
  }

  // Inter-tool LLM thinking (between tool call batches)
  if (interToolMs > 0) {
    segments.push({ label: `llm thinking ${fmtDur(interToolMs)}`, ms: interToolMs, color: "var(--lm-purple)", from: "tool_call result (batch N)", to: "tool_call start (batch N+1)" });
  }

  // Response gen (last tool result → lifecycle_end)
  if (lastToolResultTs && lifecycleEndTs) {
    const ms = lifecycleEndTs - lastToolResultTs;
    if (ms > 0) segments.push({ label: `llm response ${fmtDur(ms)}`, ms, color: "var(--lm-green)", from: "last tool_call result (lumi)", to: "lifecycle_end (openclaw)" });
  }

  // TTS latency
  if (lifecycleEndTs && ttsTs) {
    const ms = ttsTs - lifecycleEndTs;
    if (ms > 0 && ms < 30_000) segments.push({ label: `tts send ${fmtDur(ms)}`, ms, color: "#ec4899", from: "lifecycle_end (openclaw)", to: "tts_send (lumi)" });
  }

  return { total: totalMs, segments };
}

// Extract total duration (ms) from a turn's start/end times.
export function turnDurationMs(turn: Turn): number {
  if (!turn.startTime || !turn.endTime) return 0;
  const ms = new Date(turn.endTime).getTime() - new Date(turn.startTime).getTime();
  return ms > 0 ? ms : 0;
}

// Time-to-first-token: turn start → first thinking/assistant_delta event.
// Matches what the chat page bubble stamp records on the client (first delta
// arrival ≈ moment the user sees a reply begin), so the two views are
// comparable without subtracting tail streaming + lifecycle close.
// Sourced from either live monitorBus deltas or the persisted JSONL marker
// flow events (agent_first_token / thinking_first_token), whichever lands
// first in the turn's event list.
export function turnFirstTokenMs(turn: Turn): number {
  if (!turn.startTime) return 0;
  const start = new Date(turn.startTime).getTime();
  for (const ev of turn.events) {
    const isLiveDelta = ev.type === "thinking" || ev.type === "assistant_delta";
    const isMarker = ev.type === "flow_event"
      && (ev.detail?.node === "agent_first_token" || ev.detail?.node === "thinking_first_token");
    if (isLiveDelta || isMarker) {
      const ms = new Date(ev.time).getTime() - start;
      return ms > 0 ? ms : 0;
    }
  }
  return 0;
}

// Extract billed tokens from a turn's token_usage event.
// Billed = input + cache_write + ceil(cache_read * 0.1) + output.
export function turnBilledTokens(turn: Turn): number {
  for (const ev of turn.events) {
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const u = (ev.detail as Record<string, any>)?.data;
      const inTok = Number(u?.input_tokens ?? 0);
      const outTok = Number(u?.output_tokens ?? 0);
      const cacheRead = Number(u?.cache_read_tokens ?? 0);
      const cacheWrite = Number(u?.cache_write_tokens ?? 0);
      return inTok + cacheWrite + Math.round(cacheRead * 0.1) + outTok;
    }
  }
  return 0;
}

// Extract input/output summary from a turn
export function turnIO(turn: Turn): { input: string; output: string; hwOutput: string; snapshotUrls: string[] } {
  let input = "";
  let output = "";
  let outputFromIntent = false;
  let hwOutput = "";
  const snapshotUrls: string[] = [];
  const turnRunId = turn.runId;
  for (const ev of turn.events) {
    const evRunId = extractEventRunId(ev);
    const sameRun = !turnRunId || !evRunId || evRunId === turnRunId;
    if (ev.type === "sensing_input" || (ev.type === "flow_enter" && ev.detail?.node === "sensing_input")
        || (ev.type === "flow_event" && ev.detail?.node === "sensing_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const dataMsg = d?.data?.message ?? d?.message;
      if (!input) {
        const m = ev.summary.match(/^\[([^\]]+)\]\s*(.*)/);
        input = dataMsg || (m ? m[2] : "") || ev.summary;
      }
      // Extract snapshot paths from sensing_input (backend strips [snapshot:...] from chat_send
      // text, so sensing_input is the authoritative source for the Monitor turn-item thumbnails).
      if (typeof dataMsg === "string") {
        const snapRe = /\[snapshot:\s*(?:\/tmp\/lumi-(?:sensing|emotion|motion)-snapshots|\/var\/log\/lumi\/snapshots|\/var\/lib\/lelamp\/snapshots)\/((?:sensing|emotion|motion)_[^\]]+\.jpg)\]/g;
        let snapMatch;
        while ((snapMatch = snapRe.exec(dataMsg)) !== null) {
          const url = `/api/sensing/snapshot/${snapMatch[1]}`;
          if (!snapshotUrls.includes(url)) snapshotUrls.push(url);
        }
      }
    }
    if (ev.type === "chat_input" || (ev.type === "flow_event" && ev.detail?.node === "chat_input")) {
      const d = ev.detail as Record<string, any> | undefined;
      const fullMsg = d?.message ?? d?.data?.message;
      const sender = d?.sender ?? d?.data?.sender;
      const msg = fullMsg || parseChannelSummary(ev.summary);
      if (msg) {
        input = sender ? `[${sender}] ${msg}` : msg;
      } else if (!input) {
        input = CHANNEL_FALLBACK_MESSAGE;
      }
    }
    if (!input && turn.type.startsWith("ambient:")) {
      input = turn.type.replace("ambient:", "") + " behavior";
    }
    if (!input && (ev.type === "schedule_trigger" || ev.type === "cron_fire")) {
      const d = ev.detail as Record<string, any> | undefined;
      input = d?.name ?? d?.data?.name ?? ev.summary ?? "scheduled task";
    }
    if (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) {
      const d = ev.detail as Record<string, any> | undefined;
      const raw = (d?.data?.message ?? d?.message ?? ev.summary ?? "").trim();
      // Extract all snapshot paths → convert to API URLs.
      // Accepts sensing_*.jpg (presence), emotion_*.jpg (FER), motion_*.jpg (activity) across all 4 dirs.
      const snapRe = /\[snapshot:\s*(?:\/tmp\/lumi-(?:sensing|emotion|motion)-snapshots|\/var\/log\/lumi\/snapshots|\/var\/lib\/lelamp\/snapshots)\/((?:sensing|emotion|motion)_[^\]]+\.jpg)\]/g;
      let snapMatch;
      while ((snapMatch = snapRe.exec(raw)) !== null) {
        const url = `/api/sensing/snapshot/${snapMatch[1]}`;
        if (!snapshotUrls.includes(url)) snapshotUrls.push(url);
      }
      if (!input) {
        // Strip the sensing prefix ([sensing:<type>], [activity], [emotion], or [speech_emotion]) to get the payload body.
        const m = raw.match(/^\s*\[(?:sensing:[^\]]+|activity|emotion|speech_emotion)\]\s*(.*)$/is);
        const extracted = (m?.[1] ?? "").replace(/\n?\[snapshot:[^\]]+\]/g, "").trim();
        if (extracted) input = extracted;
      }
    }
    if (sameRun && (ev.type === "intent_match" || (ev.type === "flow_event" && ev.detail?.node === "intent_match"))) {
      const d = ev.detail as Record<string, any> | undefined;
      output = d?.data?.tts ?? d?.tts ?? ev.summary ?? output;
      outputFromIntent = true;
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      for (const a of actions) {
        const m = a.match(/^(?:POST|GET|PUT|DELETE)\s+(\/\S+)/);
        if (m && !hwOutput.includes(m[1])) {
          hwOutput += (hwOutput ? ", " : "") + m[1];
        }
      }
    }
    if (!outputFromIntent && sameRun && (ev.type === "tts" || (ev.type === "flow_event" && (ev.detail?.node === "tts_send" || ev.detail?.node === "tts_suppressed")))) {
      const d = ev.detail as Record<string, any> | undefined;
      output = d?.data?.text ?? d?.text ?? ev.summary ?? output;
    }
    if (!output && sameRun && ev.type === "chat_response" && ev.state === "final") {
      const d = ev.detail as Record<string, any> | undefined;
      output = d?.message ?? ev.summary ?? "";
    }
    // Slash command success: state:"final" with payload but no lifecycle.
    // Backend persists the message in chat_final_ok so this path doesn't
    // depend on SSE chat_response (which may not be replayed for old turns).
    // The flow event JSON puts the payload under `data.message` (top-level
    // fields are kind/node/ts/seq/trace_id), so read d.data.message first
    // and fall back to d.message for any flat shape variant.
    if (!output && sameRun && ev.type === "flow_event" && ev.detail?.node === "chat_final_ok") {
      const d = ev.detail as Record<string, any> | undefined;
      output = d?.data?.message ?? d?.message ?? "";
    }
    // Detect no_reply from flow event (persisted in JSONL, unlike SSE chat_response)
    if (!output && sameRun && ev.type === "flow_event" && ev.detail?.node === "no_reply") {
      output = "[no reply]";
    }
    if (turn.type.startsWith("ambient:") && ev.type === "flow_exit" && ev.detail?.node?.startsWith("ambient_")) {
      output = ev.summary || "done";
    }
    if (ev.type === "tool_call" || (ev.type === "flow_event" && ev.detail?.node === "tool_call")) {
      const d = ev.detail as Record<string, any> | undefined;
      const args = d?.args ?? d?.data?.args ?? "";
      if (args) {
        const argsStr = typeof args === "string" ? args : JSON.stringify(args);
        const m = argsStr.match(/(?:POST|GET|PUT|DELETE)\s+(http\S+)/i);
        if (m) {
          const endpoint = m[1].replace(/^https?:\/\/127\.0\.0\.1:\d+/, "");
          if (endpoint && !hwOutput.includes(endpoint)) {
            hwOutput += (hwOutput ? ", " : "") + endpoint;
          }
        }
      }
    }
  }
  return { input, output, hwOutput, snapshotUrls };
}

// Scan a turn for the backend-injected `[context: current_user=X]` tag and
// return X (or null if not present). The handler adds this tag to
// motion.activity, emotion.detected, and speech_emotion.detected messages so
// downstream skills attribute rows to the right user — showing it on the
// Flow card makes user-attribution visible at a glance (which is specially
// useful when stranger flicker or multi-friend scenes are being debugged).
export function turnCurrentUser(turn: Turn): string | null {
  const re = /\[context:\s*current_user=([^\]]+)\]/i;
  for (const ev of turn.events) {
    const d = ev.detail as Record<string, any> | undefined;
    const candidates: unknown[] = [
      d?.message,
      d?.data?.message,
      d?.text,
      d?.data?.text,
      ev.summary,
    ];
    for (const c of candidates) {
      if (typeof c !== "string") continue;
      const m = c.match(re);
      if (m) return m[1].trim();
    }
  }
  return null;
}

export function turnTokenStats(turn: Turn): { inTok: number; outTok: number; cacheRead: number; cacheWrite: number; total: number } | null {
  let inTok = 0;
  let outTok = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  let total = 0;

  for (const ev of turn.events) {
    if (ev.type === "flow_event" && ev.detail?.node === "token_usage") {
      const d = ev.detail as Record<string, any> | undefined;
      const u = d?.data ?? {};
      inTok = Math.max(inTok, Number(u.input_tokens ?? 0));
      outTok = Math.max(outTok, Number(u.output_tokens ?? 0));
      cacheRead = Math.max(cacheRead, Number(u.cache_read_tokens ?? 0));
      cacheWrite = Math.max(cacheWrite, Number(u.cache_write_tokens ?? 0));
      total = Math.max(total, Number(u.total_tokens ?? 0));
      continue;
    }

    if (ev.type === "lifecycle" && ev.phase === "end" && ev.detail) {
      const d = ev.detail as Record<string, any>;
      inTok = Math.max(inTok, Number(d.inputTokens ?? 0));
      outTok = Math.max(outTok, Number(d.outputTokens ?? 0));
      cacheRead = Math.max(cacheRead, Number(d.cacheRead ?? 0));
      cacheWrite = Math.max(cacheWrite, Number(d.cacheWrite ?? 0));
      total = Math.max(total, Number(d.totalTokens ?? 0));
    }
  }

  if (!inTok && !outTok && !cacheRead && !cacheWrite && !total) return null;
  if (!total && (inTok || outTok || cacheRead || cacheWrite)) {
    total = inTok + outTok + cacheRead + cacheWrite;
  }
  return { inTok, outTok, cacheRead, cacheWrite, total };
}
