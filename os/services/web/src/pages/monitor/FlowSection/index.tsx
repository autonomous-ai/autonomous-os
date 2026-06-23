import { useCallback, useMemo, useState } from "react";
import { S } from "../styles";
import { API, FLOW_EVENTS_MAX, HW } from "../types";
import type { DisplayEvent } from "../types";
import type { FlowStage } from "./types";
import { usePolling } from "../../../hooks/usePolling";
import { FLOW_NODES, SOURCE_ICON } from "./types";
import { deriveActiveStage, groupIntoTurns, turnIO, turnBilledTokens, turnDurationMs, extractSensingType, hasSensingPrefix } from "./helpers";
import { FlowDiagram } from "./FlowDiagram";
import { TurnBadge } from "./TurnBadge";
import { CanvasModal } from "./CanvasModal";
import { CompactionModal } from "./CompactionModal";
import { PipelineModal } from "./PipelineModal";

// Category → turn types mapping
const CAT_TYPES: Record<string, string[]> = {
  mic: ["voice", "voice_command", "sound", "speech_emotion", "speech_emotion.detected"],
  cam: ["motion", "motion.activity", "emotion.detected", "pose.ergo_risk", "presence.enter", "presence.leave", "presence.away", "light.level", "environment"],
  channel: ["telegram", "discord", "slack", "wechat", "channel"],
  web: ["web_chat"],
  cron: ["cron", "cron:music"],
  system: ["system", "schedule", "music.mood"],
  // Physical input from GPIO button / TTP223 touchpad / future remotes
  // (button_actions.py). Currently only head_pat fires an agent event;
  // single/triple/long press are local-only (listen cue / reboot /
  // shutdown) and never POST to /sensing/event.
  button: ["touch.head_pat"],
};
const TYPE_ICON: Record<string, string> = {
  ...SOURCE_ICON,
  voice_command: "🎙",
};
const TYPE_LABEL: Record<string, string> = {
  voice: "voice", voice_command: "cmd", sound: "sound",
  motion: "motion", "motion.activity": "activity", "emotion.detected": "emotion", "speech_emotion": "voice_emo", "speech_emotion.detected": "voice_emo", "pose.ergo_risk": "posture", "presence.enter": "enter", "presence.leave": "leave", "presence.away": "away", "touch.head_pat": "head pat",
  "light.level": "light", environment: "env", system: "sys",
  "music.mood": "mood", web_chat: "web", telegram: "channel", discord: "channel", slack: "channel", wechat: "channel", channel: "channel", schedule: "sched",
  cron: "cron", "cron:music": "🎵music",
};

// Preset sensing events for manual testing
const FAKE_EVENTS: { label: string; type: string; message: string; color: string; tag: string }[] = [
  { label: "bật đèn",          type: "voice",       message: "bật đèn",                            color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "tắt đèn",          type: "voice",       message: "tắt đèn",                            color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "reading mode",     type: "voice",       message: "reading mode",                       color: "var(--lm-green)",  tag: "LOCAL"  },
  { label: "thời tiết?",       type: "voice",       message: "hôm nay thời tiết thế nào?",         color: "var(--lm-blue)",   tag: "AGENT"  },
  { label: "kể chuyện",        type: "voice",       message: "kể cho tôi nghe một câu chuyện",     color: "var(--lm-blue)",   tag: "AGENT"  },
  { label: "motion",           type: "motion",      message: "motion detected in living room",     color: "var(--lm-amber)",  tag: "SENSE"  },
  { label: "environment",      type: "environment", message: "temperature 28C humidity 65%",       color: "var(--lm-teal)",   tag: "ENV"    },
];

export function FlowSection({
  events,
  onClearEvents,
}: {
  events: DisplayEvent[];
  onClearEvents: () => void;
}) {
  const [showCanvas, setShowCanvas] = useState(false);
  const [showCompaction, setShowCompaction] = useState(false);
  const [compactionAt, setCompactionAt] = useState<{ at: string; label: string } | null>(null);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  // Mobile-only: opens the PipelineModal full-screen. Desktop hides the
  // "View pipeline" button (CSS .lm-view-pipeline-btn) so this stays false.
  const [mobilePipelineOpen, setMobilePipelineOpen] = useState(false);
  // Opt-out model: store what user has EXCLUDED. Empty = show all.
  const [excludedTypes, setExcludedTypes] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem("os-excluded-types-v1");
      if (saved) return new Set(JSON.parse(saved));
    } catch {}
    return new Set();
  });
  const [searchText, setSearchText] = useState("");
  const [fromTime, setFromTime] = useState("");
  const [toTime, setToTime] = useState("");
  const [sortBy, setSortBy] = useState<"newest" | "oldest" | "time_desc" | "time_asc" | "tokens_desc" | "tokens_asc">("newest");
  const [filtersOpen, setFiltersOpen] = useState(false);

  // Shared button styles for the Flow Panel toolbar. Pair these with the
  // `.lm-u-btn` utility class (hover/focus/active states, theme-aware) so the
  // toolbar matches the setup/settings buttons; the inline object only sets the
  // size + the per-variant accent (amber primary, red danger).
  const flowGhostBtn = {
    fontSize: 11, padding: "4px 10px", borderRadius: 6,
    background: "transparent", border: "1px solid var(--lm-border)",
    color: "var(--lm-text-dim)", fontWeight: 600,
    whiteSpace: "nowrap" as const,
  };
  const flowPrimaryBtn = {
    ...flowGhostBtn,
    background: "var(--lm-amber-dim)", border: "1px solid var(--lm-amber)",
    color: "var(--lm-amber)", fontWeight: 700,
  };
  const flowDangerBtn = {
    ...flowGhostBtn,
    border: "1px solid var(--lm-red)", color: "var(--lm-red)", fontWeight: 700,
  };
  const flowSep = {
    width: 1, height: 18, background: "var(--lm-border)", margin: "0 2px",
  };
  const [firing, setFiring] = useState<string | null>(null);

  async function fireEvent(ev: typeof FAKE_EVENTS[0]) {
    setFiring(ev.label);
    try {
      await fetch(`${API}/sensing/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: ev.type, message: ev.message }),
      });
    } finally {
      setTimeout(() => setFiring(null), 800);
    }
  }

  const clearServerFlowLog = useCallback(async () => {
    const ok = window.confirm("Clear flow log file on server (today)? This cannot be undone.");
    if (!ok) return;
    try {
      const r = await fetch(`${API}/agent/flow-logs`, { method: "DELETE" });
      const j = await r.json();
      if (!r.ok || j?.status !== 1) throw new Error(j?.message || "request failed");

      setSelectedTurnId(null);
      onClearEvents();
      window.alert("Server flow log cleared.");
    } catch (e) {
      window.alert(`Failed to clear server flow log: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [onClearEvents]);

  const downloadUISnapshot = useCallback(() => {
    const turnsSnapshot = groupIntoTurns(events);
    const payload = {
      exportedAt: new Date().toISOString(),
      format: "os-monitor-ui-snapshot-v1",
      flowEventsWindow: FLOW_EVENTS_MAX,
      eventCount: events.length,
      turnCount: turnsSnapshot.length,
      events,
      turns: turnsSnapshot.map((t) => ({
        id: t.id,
        runId: t.runId,
        startTime: t.startTime,
        endTime: t.endTime,
        type: t.type,
        path: t.path,
        status: t.status,
        sessionBreak: t.sessionBreak,
        events: t.events,
      })),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `flow_ui_snapshot_${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [events]);

  const downloadServerJsonlTail = useCallback(async (): Promise<boolean> => {
    try {
      const r = await fetch(`${API}/agent/flow-logs?last=${FLOW_EVENTS_MAX}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const day = new Date().toISOString().slice(0, 10);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `flow_${day}_last${FLOW_EVENTS_MAX}.jsonl`;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      return true;
    } catch (e) {
      console.error(e);
      window.alert(`JSONL download failed: ${e instanceof Error ? e.message : String(e)}`);
      return false;
    }
  }, []);


  const downloadFlowBundle = useCallback(async () => {
    const jsonlOk = await downloadServerJsonlTail();
    if (jsonlOk) await new Promise((resolve) => setTimeout(resolve, 500));
    downloadUISnapshot();
  }, [downloadServerJsonlTail, downloadUISnapshot]);

  const saveExcluded = (next: Set<string>) => {
    try { localStorage.setItem("os-excluded-types-v1", JSON.stringify([...next])); } catch {}
  };

  const toggleType = (type: string) => {
    setExcludedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type); else next.add(type);
      saveExcluded(next);
      return next;
    });
  };

  const toggleCategory = (cat: string) => {
    const catTypes = CAT_TYPES[cat] ?? [];
    setExcludedTypes((prev) => {
      const allExcluded = catTypes.every((t) => prev.has(t));
      const next = new Set(prev);
      if (allExcluded) { catTypes.forEach((t) => next.delete(t)); }
      else { catTypes.forEach((t) => next.add(t)); }
      saveExcluded(next);
      return next;
    });
  };

  const turns = useMemo(() => groupIntoTurns(events), [events]);

  // Live current_user — polled from the device every 2s (same source the Users
  // tab uses). Reading from turn events instead was stale: if the agent is
  // busy and no motion/emotion event has streamed through, the last tagged
  // turn can be minutes old and show the wrong person.
  const [currentUser, setCurrentUser] = useState<string>("");
  usePolling(async (signal) => {
    const r = await fetch(`${HW}/face/current-user`, { signal });
    if (!r.ok) return;
    const j = await r.json();
    setCurrentUser(typeof j?.current_user === "string" ? j.current_user : "");
  }, 5000);

  // Sub-types that actually appear in the current turns list
  const availableTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const t of turns) seen.add(t.type);
    return [...seen];
  }, [turns]);

  const filteredTurns = useMemo(() => {
    const filtered = turns.filter((t) => {
      if (t.path === "dropped" && excludedTypes.has("__dropped")) return false;
      if (t.path !== "dropped" && excludedTypes.has(t.type)) return false;
      if (fromTime || toTime) {
        const m = t.startTime.match(/T(\d{2}:\d{2})/);
        const tt = m?.[1] ?? "";
        if (fromTime && tt < fromTime) return false;
        if (toTime && tt > toTime) return false;
      }
      if (searchText.trim()) {
        const q = searchText.toLowerCase().trim();
        const { input, output } = turnIO(t);
        if (!`${input} ${output} ${t.type} ${t.runId ?? ""} ${t.id}`.toLowerCase().includes(q)) return false;
      }
      return true;
    });
    if (sortBy === "oldest") {
      filtered.reverse();
    } else if (sortBy === "time_desc") {
      filtered.sort((a, b) => turnDurationMs(b) - turnDurationMs(a));
    } else if (sortBy === "time_asc") {
      filtered.sort((a, b) => turnDurationMs(a) - turnDurationMs(b));
    } else if (sortBy === "tokens_desc") {
      filtered.sort((a, b) => turnBilledTokens(b) - turnBilledTokens(a));
    } else if (sortBy === "tokens_asc") {
      filtered.sort((a, b) => turnBilledTokens(a) - turnBilledTokens(b));
    }
    // "newest" = default order from groupIntoTurns (newest first)
    return filtered;
  }, [turns, excludedTypes, fromTime, toTime, searchText, sortBy]);
  // Detect adjacent turn pairs where one is a device-id turn that closed with
  // chat_final_empty (OpenClaw closed stream · no message · no lifecycle) and
  // the adjacent turn is an OpenClaw-assigned UUID with matching input text.
  // Each pair gets a stable color (hashed from the device runId) so distinct
  // pairs in view are visually distinguishable. Purely visual correlation —
  // no semantic label.
  const pairTintMap = useMemo(() => {
    const map = new Map<string, string>();
    const PAIR_BGS = [
      "rgba(167, 139, 250, 0.14)", // purple
      "rgba(34, 211, 238, 0.14)",  // cyan
      "rgba(244, 114, 182, 0.14)", // pink
      "rgba(45, 212, 191, 0.14)",  // teal
      "rgba(129, 140, 248, 0.14)", // indigo
      "rgba(248, 113, 113, 0.12)", // soft red
      "rgba(132, 204, 22, 0.14)",  // lime
      "rgba(236, 72, 153, 0.12)",  // magenta
    ];
    const hashColor = (key: string) => {
      let h = 0;
      for (let i = 0; i < key.length; i++) h = ((h << 5) - h + key.charCodeAt(i)) | 0;
      return PAIR_BGS[Math.abs(h) % PAIR_BGS.length];
    };
    // Inputs of the same logical message may differ between OS-server-side and
    // OpenClaw-side because:
    //   • OS-server log truncates chat_input message at 500 chars + "…" (see
    //     service_chat.go:147) — UUID-side carries the full text.
    //   • OS-server log keeps `[snapshot: /var/...]` paths in presence events
    //     while OpenClaw refires with the snapshot stripped.
    // So check substring containment either way (after stripping the
    // sender prefix and trailing "…"). Guard with min length ≥32 to
    // avoid coincidental short-string matches.
    const normalizeForMatch = (s: string) =>
      s.replace(/^\[[^\]]+\]\s*/, "").replace(/…\s*$/, "").trim();
    const isDeviceRun = (id: string) => id.startsWith("device-");
    for (let i = 0; i < filteredTurns.length - 1; i++) {
      const a = filteredTurns[i];
      const b = filteredTurns[i + 1];
      const tryPair = (deviceTurn: typeof a, uuidTurn: typeof b) => {
        if (!isDeviceRun(deviceTurn.id) || isDeviceRun(uuidTurn.id)) return false;
        const closedEmpty = deviceTurn.events.some((ev) =>
          ev.type === "flow_event" && (
            (ev.detail as Record<string, any>)?.node === "chat_final_empty" ||
            (ev.detail as Record<string, any>)?.node === "turn_steered"
          )
        );
        if (!closedEmpty) return false;
        const deviceIn = normalizeForMatch(turnIO(deviceTurn).input);
        const uuidIn = normalizeForMatch(turnIO(uuidTurn).input);
        if (!deviceIn || !uuidIn) return false;
        if (Math.min(deviceIn.length, uuidIn.length) < 32) return false;
        if (!deviceIn.includes(uuidIn) && !uuidIn.includes(deviceIn)) return false;
        const color = hashColor(deviceTurn.id);
        map.set(a.id, color);
        map.set(b.id, color);
        return true;
      };
      tryPair(a, b) || tryPair(b, a);
    }
    return map;
  }, [filteredTurns]);
  // When user explicitly selected a turn, keep it even if new events arrive.
  // Only auto-select latest turn when nothing is selected.
  const selectedTurn = selectedTurnId
    ? (turns.find((t) => t.id === selectedTurnId) ?? turns.find((t) => t.runId === selectedTurnId))
    : filteredTurns[0];

  const turnEvents = selectedTurn?.events ?? events.slice(-30);
  const activeStage = deriveActiveStage(turnEvents);

  const visitedStages = new Set<FlowStage>();
  for (const ev of turnEvents) {
    const node = ev.detail?.node as string | undefined;
    const key = (ev.type === "flow_event" || ev.type === "flow_enter" || ev.type === "flow_exit") && node
      ? `${ev.type}:${node}`
      : ev.type;
    for (const flowNode of FLOW_NODES) {
      if (flowNode.triggers.includes(key)) visitedStages.add(flowNode.id);
    }
    // tool_exec is the FlowStage anchor for the Event Pipeline rect (see
    // FlowDiagram.tsx — its node circle is hidden, the rect is rendered in
    // its place). Treat the pipeline as "visited" whenever any agent core
    // stream event arrives — thinking / assistant deltas, lifecycle markers
    // — so the agent_call → pipeline → response edges and the pipeline →
    // hw_* edges light up correctly even on turns without explicit
    // tool_call events.
    if (ev.type === "thinking" || ev.type === "assistant_delta") {
      visitedStages.add("tool_exec");
    }
    if (ev.type === "flow_event" && (node === "lifecycle_start" || node === "lifecycle_end")) {
      visitedStages.add("tool_exec");
    }
  }
  for (const ev of turnEvents) {
    // Detect sensing type from sensing_input, chat_send, or agent_call events
    const isSensingInput = ev.type === "sensing_input" ||
      (ev.type === "flow_enter" && ev.detail?.node === "sensing_input") ||
      (ev.type === "flow_event" && ev.detail?.node === "sensing_input");
    const fromSensingChatSend = (ev.type === "chat_send" || (ev.type === "flow_event" && ev.detail?.node === "chat_send")) &&
      hasSensingPrefix(ev.summary ?? "");
    const d = ev.detail as Record<string, any> | undefined;
    const sensingType = d?.data?.type ?? d?.type;
    const fromSensingAgentCall = (ev.type === "flow_event" && ev.detail?.node === "agent_call") &&
      (sensingType === "voice" || sensingType === "voice_command" || sensingType === "motion" || sensingType === "motion.activity" || sensingType === "emotion.detected" || sensingType === "speech_emotion.detected" || sensingType === "pose.ergo_risk" || sensingType === "sound");
    if (isSensingInput || fromSensingChatSend || fromSensingAgentCall) {
      // Determine mic vs cam from sensing type or summary prefix.
      // speech_emotion.detected is mic-sourced even though its label contains "emotion".
      let detectedType = sensingType;
      if (!detectedType && ev.summary) {
        detectedType = extractSensingType(ev.summary) ?? "";
      }
      const isMicEmotion = /speech_emotion/i.test(detectedType ?? "");
      const isButton = /^touch\./i.test(detectedType ?? "");
      const isCam = !isMicEmotion && !isButton && /motion|presence|light|emotion/i.test(detectedType ?? "");
      visitedStages.add(isButton ? "button_input" : isCam ? "cam_input" : "mic_input");
      break;
    }
  }

  // HW nodes: light up when intent_match has hardware actions (local path → LED)
  if (visitedStages.has("local_match")) {
    const hasActions = turnEvents.some((ev) => {
      if (ev.type !== "intent_match" && !(ev.type === "flow_event" && ev.detail?.node === "intent_match")) return false;
      const d = ev.detail as Record<string, any> | undefined;
      const actions: string[] = d?.data?.actions ?? d?.actions ?? [];
      return actions.length > 0;
    });
    if (hasActions) visitedStages.add("hw_led");
  }

  // TTS suppressed: mark TTS as visited so it shows red via nodeColor
  const hasTtsSuppressed = turnEvents.some((ev) =>
    ev.type === "flow_event" && (ev.detail as Record<string, any>)?.node === "tts_suppressed"
  );
  if (hasTtsSuppressed) visitedStages.add("tts_speak");

  // CH OUT: only light up for channel turns with a real response (not no_reply)
  const CHANNEL_TYPES = new Set(["telegram", "discord", "slack", "wechat", "channel"]);
  if (selectedTurn && CHANNEL_TYPES.has(selectedTurn.type) && visitedStages.has("agent_response")) {
    const hasNoReply = turnEvents.some((ev) =>
      (ev.type === "flow_event" && ev.detail?.node === "no_reply") ||
      (ev.type === "chat_response" && ev.summary === "[no reply]")
    );
    if (!hasNoReply) {
      visitedStages.add("tg_out");
    }
  }

  // Pipeline body — header (label + summary-prompt button + meta) + timing
  // breakdown + FlowDiagram. Shared between the desktop inline render
  // (wrapped in S.card inside .lm-flow-pipeline) and the mobile
  // PipelineModal (full-screen overlay reached from the per-turn "View
  // pipeline" button). Captured here so both call sites stay in sync.
  const pipelineBody = (
    <>
      <div style={{ marginBottom: 10, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" as const }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" as const }}>
          <span style={S.cardLabel}>Turn Pipeline</span>
          {selectedTurn && (
            <button
              onClick={() => setCompactionAt({
                at: selectedTurn.startTime,
                label: `${selectedTurn.type} @ ${new Date(selectedTurn.startTime).toLocaleTimeString()}`,
              })}
              title="Show the Agent compaction summary that was active at the moment this turn fired — the text injected at the top of this turn's prompt."
              style={{
                fontSize: 10, padding: "2px 8px", borderRadius: 4,
                background: "var(--lm-purple)", border: "1px solid var(--lm-purple)",
                color: "#fff", cursor: "pointer", fontWeight: 700,
              }}
            >
              📋 summary prompt of this turn
            </button>
          )}
        </div>
        {selectedTurn && (
          <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
            {selectedTurn.type} · {selectedTurn.events.length} events
            {selectedTurn.endTime ? ` · done` : ` · active`}
          </span>
        )}
      </div>
      <FlowDiagram activeStage={activeStage} visitedStages={visitedStages} turnEvents={turnEvents} compact />
    </>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, height: "100%", overflow: "hidden" }}>
      {showCanvas && (
        <CanvasModal
          activeStage={activeStage}
          visitedStages={visitedStages}
          turnEvents={turnEvents}
          onClose={() => setShowCanvas(false)}
        />
      )}

      {mobilePipelineOpen && selectedTurn && (
        <PipelineModal onClose={() => setMobilePipelineOpen(false)}>
          {pipelineBody}
        </PipelineModal>
      )}

      {showCompaction && <CompactionModal onClose={() => setShowCompaction(false)} />}
      {compactionAt && (
        <CompactionModal
          at={compactionAt.at}
          turnLabel={compactionAt.label}
          onClose={() => setCompactionAt(null)}
        />
      )}

      {/* Header card — neutral toolbar with one primary action (Canvas)
          and one destructive (Clear). All other actions share the same
          ghost-button style so the eye lands on the meaningful color,
          not a rainbow of competing fills. */}
      <div style={{ ...S.card, padding: "10px 14px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" as const, gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" as const }}>
            <span style={S.cardLabel}>Flow Panel</span>
            {currentUser && (() => {
              const isUnknown = currentUser === "unknown";
              const color = isUnknown ? "var(--lm-text-muted)" : "var(--lm-teal)";
              return (
                <span
                  title={isUnknown ? "Device currently sees only strangers" : `Device's current user: ${currentUser}`}
                  style={{
                    fontSize: 11, padding: "3px 9px", borderRadius: 6,
                    background: `${color}18`, color,
                    fontWeight: 700, textTransform: "capitalize",
                    border: `1px solid ${color}55`,
                  }}
                >👤 {currentUser}</span>
              );
            })()}
          </div>

          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 6, alignItems: "center" }}>
            {/* Modals — Canvas is the primary visual entry, Summary is a
                deep-dive button next to it. */}
            <button
              onClick={() => setShowCompaction(true)}
              title={
                "Xem 'bộ nhớ tóm tắt' mà Agent tự sinh và chèn vào đầu prompt của MỖI turn agent.\n\n" +
                "• Vì sao cần: khi context vượt ~80k tokens, Agent auto-compact — gộp history cũ thành 1 đoạn summary, rồi dùng summary này thay cho history đến lần compact tiếp theo.\n" +
                "• Rủi ro: nếu summary vô tình copy/méo rule từ SKILL.md, KNOWLEDGE.md, SOUL.md → agent sẽ theo summary (đứng đầu prompt) thay vì SKILL.md → trợ lý trả lời sai lý do không giải thích nổi.\n\n" +
                "Click để xem: timestamp, summary chars, session file, và TOÀN VĂN summary đang điều khiển trợ lý."
              }
              className="lm-u-btn"
              style={flowGhostBtn}
            >📋 Summary</button>
            <button
              onClick={() => setShowCanvas(true)}
              title="Open the flow canvas — a stacked timeline of all turns."
              className="lm-u-btn"
              style={flowPrimaryBtn}
            >⬢ Canvas</button>

            <div style={flowSep} />

            {/* Downloads */}
            <button
              type="button"
              onClick={() => void downloadFlowBundle()}
              title={`Downloads 3 files: (1) server JSONL last ${FLOW_EVENTS_MAX} lines — same tail as this panel; (2) UI snapshot JSON (events + turns); (3) Agent debug payload JSONL.`}
              className="lm-u-btn"
              style={flowGhostBtn}
            >↓ Bundle</button>
            <a
              href={`${API}/agent/flow-logs`}
              download
              title="Full day JSONL on server (all lines today — wider than the panel window)"
              className="lm-u-btn"
              style={{ ...flowGhostBtn, textDecoration: "none", display: "inline-flex", alignItems: "center" }}
            >📅 Full day</a>

            <div style={flowSep} />

            {/* Destructive */}
            <button
              onClick={clearServerFlowLog}
              title="Clear server flow log + Agent debug logs"
              className="lm-u-btn"
              style={flowDangerBtn}
            >🗑 Clear</button>
          </div>
        </div>
      </div>

      {/* Simulate card — hidden for now */}
      {false && window.location.hostname === "localhost" && (
        <div style={{ ...S.card, padding: "10px 14px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
            <span style={S.cardLabel}>Simulate Event</span>
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>dev only · fires POST /sensing/event on device</span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 6 }}>
            {FAKE_EVENTS.map((ev) => (
              <button
                key={ev.label}
                onClick={() => fireEvent(ev)}
                disabled={firing !== null}
                style={{
                  fontSize: 11, padding: "4px 11px", borderRadius: 6, cursor: "pointer",
                  background: firing === ev.label ? `${ev.color}25` : "var(--lm-surface)",
                  border: `1px solid ${firing === ev.label ? ev.color : "var(--lm-border)"}`,
                  color: firing === ev.label ? ev.color : "var(--lm-text-dim)",
                  fontWeight: 600, transition: "all 0.15s",
                  display: "flex", alignItems: "center", gap: 5,
                }}
              >
                <span style={{
                  fontSize: 9, padding: "1px 4px", borderRadius: 3,
                  background: `${ev.color}20`, color: ev.color, fontWeight: 700,
                }}>{ev.tag}</span>
                {firing === ev.label ? "…" : ev.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Flow diagram + turn list */}
      <div className="lm-flow-layout" style={{ display: "flex", gap: 14, flex: 1, minHeight: 0 }}>

        {/* Turn history list. Width is driven by the `.lm-flow-turns` CSS class
            (clamps wider on big screens so the dense IN/REPLY text is readable,
            and the canvas keeps less dead space) rather than a fixed inline px. */}
        <div className="lm-flow-turns" style={{
          ...S.card,
          flexShrink: 0,
          display: "flex",
          flexDirection: "column" as const,
          minHeight: 0,
          padding: 0,
          overflow: "hidden",
        }}>
          <div style={{ padding: "10px 12px 8px", borderBottom: "1px solid var(--lm-border)" }}>
            {/* Title + count + filters toggle.
                Primary row stays compact: identity (Turns N/M) + a single
                toggle that reveals advanced filters. Avoids the 6-row
                tall header that earlier crowded the list area. */}
            <div style={{ display: "flex", alignItems: "center", marginBottom: 6, gap: 6 }}>
              <span style={S.cardLabel}>Turns</span>
              <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                {filteredTurns.length}/{turns.length}
              </span>
              {(() => {
                const activeFilters =
                  (searchText.trim() ? 1 : 0) +
                  (fromTime || toTime ? 1 : 0) +
                  (sortBy !== "newest" ? 1 : 0) +
                  (availableTypes.filter((t) => excludedTypes.has(t)).length > 0 ? 1 : 0);
                return (
                  <button
                    onClick={() => setFiltersOpen((v) => !v)}
                    style={{
                      marginLeft: "auto", padding: "2px 8px", borderRadius: 4, fontSize: 10,
                      cursor: "pointer", fontWeight: 600,
                      border: `1px solid ${activeFilters > 0 ? "var(--lm-amber)" : "var(--lm-border)"}`,
                      background: activeFilters > 0 ? "var(--lm-amber-dim)" : "transparent",
                      color: activeFilters > 0 ? "var(--lm-amber)" : "var(--lm-text-muted)",
                      display: "inline-flex", alignItems: "center", gap: 4,
                    }}
                    title={filtersOpen ? "Hide filters" : "Show filters"}
                  >
                    Filters{activeFilters > 0 ? ` · ${activeFilters}` : ""}
                    <span style={{ fontSize: 8, transition: "transform 0.15s", transform: filtersOpen ? "rotate(180deg)" : "none" }}>▾</span>
                  </button>
                );
              })()}
            </div>

            {/* Search — always visible (most common quick-filter). */}
            <input
              type="text"
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              placeholder="🔍 search input / output…"
              className="lm-u-input"
              style={{
                width: "100%", boxSizing: "border-box" as const,
                padding: "5px 9px", borderRadius: 5, fontSize: 11,
                marginBottom: 6, outline: "none",
              }}
            />

            {/* Category quick-toggle — always visible (primary filter UX). */}
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" as const }}>
              {([
                { key: "mic", icon: "🎤", label: "Mic" },
                { key: "cam", icon: "👁", label: "Cam" },
                { key: "button", icon: "✋", label: "Btn" },
                { key: "channel", icon: "💬", label: "CH" },
                { key: "web", icon: "🖥", label: "Web" },
                { key: "cron", icon: "⏰", label: "Cron" },
                { key: "system", icon: "⚙", label: "Sys" },
              ] as const).map((f) => {
                const catTypes = CAT_TYPES[f.key] ?? [];
                const available = catTypes.filter((t) => availableTypes.includes(t));
                const active = available.length > 0 && available.every((t) => !excludedTypes.has(t));
                const partial = !active && available.some((t) => !excludedTypes.has(t));
                const border = active ? "var(--lm-amber)" : partial ? "var(--lm-teal)" : "var(--lm-border)";
                const color = active ? "var(--lm-amber)" : partial ? "var(--lm-teal)" : "var(--lm-text-muted)";
                return (
                  <button key={f.key} onClick={() => toggleCategory(f.key)} style={{
                    padding: "3px 8px", borderRadius: 4, fontSize: 10, cursor: "pointer",
                    border: `1px solid ${border}`,
                    background: active ? "var(--lm-amber-dim)" : partial ? "var(--lm-teal-dim)" : "transparent",
                    color, fontWeight: active || partial ? 600 : 400,
                  }}>
                    {f.icon} {f.label}
                  </button>
                );
              })}
              {/* Dropped — appears inline when relevant */}
              {turns.some((t) => t.path === "dropped") && (() => {
                const on = !excludedTypes.has("__dropped");
                return (
                  <button onClick={() => toggleType("__dropped")} style={{
                    padding: "3px 8px", borderRadius: 4, fontSize: 10, cursor: "pointer",
                    border: `1px solid ${on ? "var(--lm-red)" : "var(--lm-border)"}`,
                    background: on ? "var(--lm-red-dim)" : "transparent",
                    color: on ? "var(--lm-red)" : "var(--lm-text-muted)",
                    fontWeight: on ? 600 : 400,
                  }}>
                    ⏸ Dropped
                  </button>
                );
              })()}
            </div>

            {/* Advanced filters: sort + sub-types + time range. Hidden by
                default; click "Filters" to expand. Keeps power-user controls
                accessible without dominating the header. */}
            {filtersOpen && (
              <div style={{
                marginTop: 8, paddingTop: 8, borderTop: "1px dashed var(--lm-border)",
                display: "flex", flexDirection: "column" as const, gap: 8,
              }}>
                {/* Sort */}
                <div>
                  <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginBottom: 3, textTransform: "uppercase", letterSpacing: "0.06em" }}>Sort</div>
                  <div style={{ display: "flex", gap: 3, flexWrap: "wrap" as const }}>
                    {([
                      { key: "newest", label: "Newest" },
                      { key: "oldest", label: "Oldest" },
                      { key: "time_desc", label: "Slowest" },
                      { key: "time_asc", label: "Fastest" },
                      { key: "tokens_desc", label: "↑ Tokens" },
                      { key: "tokens_asc", label: "↓ Tokens" },
                    ] as const).map((s) => (
                      <button
                        key={s.key}
                        onClick={() => setSortBy(s.key)}
                        style={{
                          padding: "2px 7px", borderRadius: 3, fontSize: 10, cursor: "pointer",
                          border: `1px solid ${sortBy === s.key ? "var(--lm-amber)" : "var(--lm-border)"}`,
                          background: sortBy === s.key ? "var(--lm-amber-dim)" : "transparent",
                          color: sortBy === s.key ? "var(--lm-amber)" : "var(--lm-text-muted)",
                          fontWeight: sortBy === s.key ? 600 : 400,
                        }}
                      >{s.label}</button>
                    ))}
                  </div>
                </div>

                {/* Sub-types */}
                {availableTypes.length > 0 && (
                  <div>
                    <div style={{
                      display: "flex", alignItems: "center", justifyContent: "space-between",
                      fontSize: 9, color: "var(--lm-text-muted)", marginBottom: 3,
                      textTransform: "uppercase", letterSpacing: "0.06em",
                    }}>
                      <span>Sub-types</span>
                      {(() => {
                        const allOn = availableTypes.every((t) => !excludedTypes.has(t));
                        return (
                          <button
                            onClick={() => {
                              setExcludedTypes((prev) => {
                                const next = new Set(prev);
                                if (allOn) { availableTypes.forEach((t) => next.add(t)); }
                                else { availableTypes.forEach((t) => next.delete(t)); }
                                saveExcluded(next);
                                return next;
                              });
                            }}
                            style={{
                              padding: "1px 6px", borderRadius: 3, fontSize: 9, cursor: "pointer", fontWeight: 600,
                              border: `1px solid ${allOn ? "var(--lm-amber)" : "var(--lm-border)"}`,
                              background: allOn ? "var(--lm-amber-dim)" : "transparent",
                              color: allOn ? "var(--lm-amber)" : "var(--lm-text-muted)",
                              textTransform: "none", letterSpacing: 0,
                            }}
                          >{allOn ? "All on" : "Enable all"}</button>
                        );
                      })()}
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 3 }}>
                      {availableTypes.map((type) => {
                        const on = !excludedTypes.has(type);
                        const icon = TYPE_ICON[type] ?? "•";
                        const label = TYPE_LABEL[type] ?? type.replace("ambient:", "~");
                        return (
                          <button key={type} onClick={() => toggleType(type)} title={type} style={{
                            padding: "2px 6px", borderRadius: 3, fontSize: 10, cursor: "pointer",
                            border: `1px solid ${on ? "var(--lm-teal)" : "var(--lm-border)"}`,
                            background: on ? "var(--lm-teal-dim)" : "transparent",
                            color: on ? "var(--lm-teal)" : "var(--lm-text-muted)",
                            fontWeight: on ? 600 : 400,
                          }}>
                            {icon} {label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Time range */}
                <div>
                  <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginBottom: 3, textTransform: "uppercase", letterSpacing: "0.06em" }}>Time range</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                    <input
                      type="time"
                      value={fromTime}
                      onChange={(e) => setFromTime(e.target.value)}
                      style={{
                        flex: 1, padding: "3px 6px", borderRadius: 4, fontSize: 10,
                        background: "var(--lm-bg)", border: "1px solid var(--lm-border)",
                        color: fromTime ? "var(--lm-text)" : "var(--lm-text-muted)", outline: "none",
                      }}
                    />
                    <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>→</span>
                    <input
                      type="time"
                      value={toTime}
                      onChange={(e) => setToTime(e.target.value)}
                      style={{
                        flex: 1, padding: "3px 6px", borderRadius: 4, fontSize: 10,
                        background: "var(--lm-bg)", border: "1px solid var(--lm-border)",
                        color: toTime ? "var(--lm-text)" : "var(--lm-text-muted)", outline: "none",
                      }}
                    />
                    {(fromTime || toTime) && (
                      <button onClick={() => { setFromTime(""); setToTime(""); }} style={{
                        padding: "2px 6px", borderRadius: 3, fontSize: 10, cursor: "pointer",
                        border: "1px solid var(--lm-border)", background: "transparent",
                        color: "var(--lm-red)", fontWeight: 700,
                      }}>✕</button>
                    )}
                  </div>
                </div>

                {/* Reset all */}
                <button
                  onClick={() => {
                    setSearchText(""); setFromTime(""); setToTime(""); setSortBy("newest");
                    setExcludedTypes(() => { saveExcluded(new Set()); return new Set(); });
                  }}
                  style={{
                    alignSelf: "flex-start", padding: "3px 9px", borderRadius: 4, fontSize: 10,
                    cursor: "pointer", border: "1px solid var(--lm-border)", background: "transparent",
                    color: "var(--lm-text-muted)",
                  }}
                >Reset all</button>
              </div>
            )}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: "6px 8px", display: "flex", flexDirection: "column", gap: 5 }} className="lm-hide-scroll">
            {filteredTurns.length === 0 ? (
              <div style={{ padding: 12, color: "var(--lm-text-muted)", fontSize: 11 }}>No turns match filter</div>
            ) : (
              filteredTurns.map((turn, i) => (
                <div key={turn.id}>
                  {i > 0 && filteredTurns[i - 1].sessionBreak && (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 8, padding: "4px 0", margin: "2px 0",
                    }}>
                      <div style={{ flex: 1, borderTop: "1px dashed var(--lm-text-muted)", opacity: 0.4 }} />
                      <span style={{ fontSize: 8, color: "var(--lm-text-muted)", whiteSpace: "nowrap" }}>session</span>
                      <div style={{ flex: 1, borderTop: "1px dashed var(--lm-text-muted)", opacity: 0.4 }} />
                    </div>
                  )}
                  <div
                    className="lm-turn-card"
                    data-expanded={turn.id === selectedTurn?.id ? "true" : "false"}
                    onClick={() => setSelectedTurnId(turn.id === selectedTurn?.id ? null : turn.id)}
                    style={{
                      borderRadius: 8,
                      outline: turn.id === selectedTurn?.id ? `2px solid var(--lm-amber)` : "none",
                      cursor: "pointer",
                    }}
                  >
                    <TurnBadge
                      turn={turn}
                      pairTint={pairTintMap.get(turn.id)}
                      onViewPipeline={() => {
                        setSelectedTurnId(turn.id);
                        setMobilePipelineOpen(true);
                      }}
                    />
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Center: flow diagram. Hidden on mobile via .lm-flow-pipeline CSS —
            users reach it through the "View pipeline" button on each
            TurnBadge, which opens PipelineModal full-screen with the same
            pipelineBody content. */}
        <div className="lm-flow-pipeline" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 12, minHeight: 0 }}>
          <div style={{ ...S.card, flex: 1, minHeight: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
            {pipelineBody}
          </div>
        </div>

      </div>
    </div>
  );
}
