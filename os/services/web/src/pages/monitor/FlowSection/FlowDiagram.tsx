import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, Timer } from "lucide-react";
import type { DisplayEvent } from "../types";
import type { FlowStage, ActiveFlowStage } from "./types";
import { FLOW_NODES } from "./types";
import { useTheme } from "@/lib/useTheme";
import { extractNodeInfo, aggregateEvents } from "./helpers";

// Hidden-textarea clipboard fallback for non-secure origins (http://Pi.local).
// navigator.clipboard.writeText only works in secure contexts; without this,
// the pipeline copy button silently no-ops when the monitor is opened over
// plain HTTP from the device.
function fallbackCopy(text: string): void {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* nothing to do */ }
  document.body.removeChild(ta);
}

export function FlowDiagram({
  activeStage,
  visitedStages,
  compact = false,
  turnEvents = [],
}: {
  activeStage: ActiveFlowStage;
  visitedStages: Set<FlowStage>;
  compact?: boolean;
  turnEvents?: DisplayEvent[];
}) {
  const VW = 1200;
  const VH = 1080;
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [, , themeClass] = useTheme();

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [pipelineGuideOpen, setPipelineGuideOpen] = useState(false);
  const [pipelineCopied, setPipelineCopied] = useState(false);
  const dragStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const svgRef = useRef<SVGSVGElement>(null);

  // Use native wheel listener with { passive: false } so preventDefault actually works
  // and stops scroll from bubbling to parent (Turns list).
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? -0.1 : 0.1;
      setZoom((z) => Math.min(4, Math.max(0.4, z + delta)));
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, []);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    setDragging(true);
    dragStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
  }, [pan]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!dragging) return;
    const dx = e.clientX - dragStart.current.x;
    const dy = e.clientY - dragStart.current.y;
    setPan({ x: dragStart.current.panX + dx / zoom, y: dragStart.current.panY + dy / zoom });
  }, [dragging, zoom]);

  const handleMouseUp = useCallback(() => setDragging(false), []);

  const resetView = useCallback(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, []);

  const vbW = VW / zoom;
  const vbH = VH / zoom;
  const vbX = (VW - vbW) / 2 - pan.x;
  const vbY = (VH - vbH) / 2 - pan.y;

  // Event Pipeline rect — centered between agent_call (950,240) and
  // agent_response (950,795). Shared between the rect rendering, the
  // tool_exec edge anchoring (so HW edges latch to the rect boundary
  // instead of a phantom point inside it), and the foreignObject row
  // list. Update one place, all three follow.
  const PIPE = { x: 798, y: 330, w: 304, h: 376 };
  // Returns the point on the pipeline rect boundary along the line from
  // the rect center toward (extX, extY). Used to anchor edges that go
  // to/from tool_exec — the conceptual "tool" anchor is the whole rect,
  // not a circle, so each edge meets the rect at its closest edge.
  const pipeAnchor = (extX: number, extY: number) => {
    const cx = PIPE.x + PIPE.w / 2;
    const cy = PIPE.y + PIPE.h / 2;
    const dx = extX - cx;
    const dy = extY - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    const tx = dx === 0 ? Infinity : (PIPE.w / 2) / Math.abs(dx);
    const ty = dy === 0 ? Infinity : (PIPE.h / 2) / Math.abs(dy);
    const t = Math.min(tx, ty);
    return { x: cx + dx * t, y: cy + dy * t };
  };

  const positions: Record<FlowStage, { x: number; y: number }> = {
    // OS server — top row
    intent_check:      { x: 80, y: 50 },
    local_match:       { x: 200, y: 50 },
    os_gate:         { x: 467, y: 795 },
    // Device — input row (MIC/CAM/BTN)
    mic_input:         { x: -40, y: 240 },
    cam_input:         { x: 80, y: 240 },
    // Button / touch input — physical interaction (GPIO button, TTP223
    // touchpad). Sits below mic so it doesn't crowd the input row and
    // its edge to intent_check has a clear path up-right past mic.
    button_input:      { x: -40, y: 350 },
    // Device — output column (stacked vertically, same x, gap=135)
    hw_emotion:        { x: 200, y: 390 },
    hw_led:            { x: 200, y: 525 },
    hw_servo:          { x: 200, y: 660 },
    hw_audio:          { x: 200, y: 795 },
    // OS-server-side log writes — stack BELOW the BCAST node (tg_alert at y=930)
    // so HOOK / BCAST stay grouped at the top of the OS-server column and the
    // three async-POST logs hang off the bottom in their own block.
    // x=467 same column. Edges from os_gate use elbow routing
    // (right → down → left) to avoid running through tg_alert.
    hw_mood:             { x: 467, y: 1065 },
    hw_wellbeing:        { x: 467, y: 1200 },
    hw_music_suggestion: { x: 467, y: 1335 },
    hw_posture:          { x: 467, y: 1470 },
    tts_speak:         { x: 200, y: 930 },
    // OpenClaw — agent core (cron lives in OpenClaw, fires agent_call).
    // The 2 inner nodes (agent_thinking, tool_exec) are rendered as a
    // single Event Pipeline rect between agent_call and agent_response —
    // see <EventPipeline> below. Their FlowStage entries are kept
    // (visited tracking, edges, info maps still reference them) but
    // their node circles are not drawn. tool_exec is the visible edge
    // anchor for HW outgoing edges; put it on the LEFT edge of the
    // pipeline rect (closest to the hw_* column at x=200) so those 5
    // edges stay visually short. agent_thinking is an inert anchor.
    schedule_trigger:  { x: 750, y: 240 },
    agent_call:        { x: 950, y: 240 },
    agent_thinking:    { x: 1180, y: 480 },
    tool_exec:         { x: 820, y: 600 },
    agent_response:    { x: 950, y: 795 },
    // External channels — outside OpenClaw
    channel_input:     { x: 1300, y: 240 },
    webchat_input:     { x: 1300, y: 440 },
    tg_out:            { x: 1300, y: 795 },
    tg_alert:          { x: 467, y: 930 },
  };

  const edges: [FlowStage, FlowStage][] = [
    ["mic_input",         "intent_check"],
    ["cam_input",         "intent_check"],
    ["button_input",      "intent_check"],
    ["intent_check",      "local_match"],
    ["local_match",       "hw_emotion"],
    ["local_match",       "hw_led"],
    ["local_match",       "hw_servo"],
    ["local_match",       "tts_speak"],
    ["intent_check",      "agent_call"],
    ["channel_input",     "agent_call"],
    ["webchat_input",     "agent_call"],
    ["schedule_trigger",  "agent_call"],
    // agent_call → pipeline → agent_response. The pipeline is a single
    // visual rect (rendered below) containing aggregated event rows.
    // tool_exec sits at the right edge of that rect so HW edges look like
    // they originate from the pipeline.
    ["agent_call",        "tool_exec"],
    ["tool_exec",         "agent_response"],
    ["tool_exec",         "hw_led"],
    ["tool_exec",         "hw_servo"],
    ["tool_exec",         "hw_emotion"],
    ["tool_exec",         "hw_audio"],
    ["tool_exec",         "os_gate"],
    ["agent_response",    "os_gate"],
    ["os_gate",         "hw_emotion"],
    ["os_gate",         "hw_led"],
    ["os_gate",         "hw_servo"],
    ["os_gate",         "hw_audio"],
    ["os_gate",         "hw_wellbeing"],
    ["os_gate",         "hw_mood"],
    ["os_gate",         "hw_music_suggestion"],
    ["os_gate",         "hw_posture"],
    ["os_gate",         "tts_speak"],
    ["os_gate",         "tg_out"],
    ["os_gate",         "tg_alert"],
    ["tg_alert",          "tg_out"],
  ];

  const nodeR = compact ? 28 : 38;
  const gateR = compact ? 22 : 30;

  const ttsSuppressed = turnEvents.some((ev) =>
    ev.type === "flow_event" && (ev.detail as Record<string, any>)?.node === "tts_suppressed"
  );

  function nodeColor(id: FlowStage) {
    if (id === "tts_speak" && ttsSuppressed) return "var(--lm-red)";
    if (id === activeStage || visitedStages.has(id)) {
      return FLOW_NODES.find((n) => n.id === id)?.color ?? "var(--lm-text-muted)";
    }
    return "var(--lm-text-muted)";
  }
  function nodeOpacity(id: FlowStage) {
    if (id === activeStage) return 1;
    if (visitedStages.has(id)) return 1;
    return 1;
  }
  function edgeColor(from: FlowStage, to: FlowStage) {
    const fromVisited = visitedStages.has(from) || from === activeStage;
    const toVisited = visitedStages.has(to) || to === activeStage;
    if (fromVisited && toVisited) return nodeColor(to);
    if (fromVisited || toVisited) return "var(--lm-border-hi)";
    return "var(--lm-border)";
  }
  function edgeOpacity(from: FlowStage, to: FlowStage) {
    const fromVisited = visitedStages.has(from) || from === activeStage;
    const toVisited = visitedStages.has(to) || to === activeStage;
    if (fromVisited && toVisited) return 0.98;
    if (fromVisited || toVisited) return 0.8;
    return 0.45;
  }

  const glowId = compact ? "flow-glow-c" : "flow-glow";

  const nodeInfo = extractNodeInfo(turnEvents);

  // Extract snapshot URLs from agent_call lines (🖼 added by helpers.ts from sensing_input or chat_send).
  const snapshotUrls: string[] = (nodeInfo.agent_call ?? [])
    .filter((l) => l.startsWith("🖼"))
    .map((l) => l.match(/snapshot:\s*\/tmp\/(?:lamp|hal)-(?:sensing|emotion|motion)-snapshots\/((?:sensing|emotion|motion)_[^\s]+\.jpg)/)?.[1])
    .filter((f): f is string => !!f)
    .map((f) => `/api/sensing/snapshot/${f}`);

  // Check if image was actually sent to agent (has_image in chat_send event)
  const imageSentToAgent: boolean = (nodeInfo.agent_call ?? []).some((l) => l.includes("📷 image attached"));

  return (
    <div id="FLOW_DIAGRAM" data-region="FLOW_DIAGRAM" style={{ position: "relative", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <svg
        ref={svgRef}
        id="FLOW_DIAGRAM_SVG" data-region="FLOW_DIAGRAM_SVG"
        viewBox={`${vbX} ${vbY} ${vbW} ${vbH}`}
        style={{
          display: "block", width: "100%", flex: 1, minHeight: 0,
          cursor: dragging ? "grabbing" : "grab", userSelect: dragging ? "none" : "auto",
        }}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
      >
        <defs>
          <filter id={glowId}>
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <marker id={`arrow-${compact ? "c" : "f"}`} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
            <path d="M0,0 L0,6 L6,3 z" fill="context-stroke" />
          </marker>
        </defs>

        {/* Cluster group backgrounds */}
        <g>
          <rect x={-100} y={0} width={1500} height={110} rx={14}
            fill="var(--lm-teal)" fillOpacity={0.12} stroke="var(--lm-teal)" strokeWidth={2} opacity={0.6}
            strokeDasharray="4 4"
          />
          <rect x={417} y={100} width={110} height={1280} rx={10}
            fill="var(--lm-teal)" fillOpacity={0.12} stroke="var(--lm-teal)" strokeWidth={2} opacity={0.6}
            strokeDasharray="4 4"
          />
          <text x={467} y={-8} textAnchor="middle"
            fill="var(--lm-teal)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            OS Server
          </text>
        </g>
        <g>
          <rect x={-100} y={185} width={360} height={805} rx={14}
            fill="var(--lm-amber)" fillOpacity={0.04} stroke="var(--lm-amber)" strokeWidth={1} opacity={0.3}
            strokeDasharray="4 4"
          />
          <text x={80} y={175} textAnchor="middle"
            fill="var(--lm-amber)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            Device
          </text>
        </g>
        <g>
          <rect x={695} y={185} width={520} height={665} rx={14}
            fill="var(--lm-blue)" fillOpacity={0.04} stroke="var(--lm-blue)" strokeWidth={1} opacity={0.3}
            strokeDasharray="4 4"
          />
          <text x={955} y={175} textAnchor="middle"
            fill="var(--lm-blue)" fontSize={11} fontWeight={700}
            fontFamily="monospace" opacity={0.6}
            style={{ letterSpacing: "0.08em" }}>
            Agent
          </text>
        </g>

        {/* Edges */}
        {edges.map(([from, to]) => {
          const f = positions[from];
          const t = positions[to];
          const color = edgeColor(from, to);
          const sw = edgeOpacity(from, to) > 0.5 ? 2 : 1.5;
          const op = edgeOpacity(from, to);
          const marker = `url(#arrow-${compact ? "c" : "f"})`;

          // Elbow edges: LOCAL → output nodes (bypass intermediate nodes)
          // Route: go right from LOCAL, then down, then left into target node
          if (from === "local_match" && (to === "hw_led" || to === "hw_servo" || to === "tts_speak" || to === "hw_emotion" || to === "hw_audio")) {
            const elbowX = t.x - 80; // offset left of target
            const startY = f.y + nodeR;
            const endY = t.y;
            const endX = t.x - nodeR - 4; // enter from left side
            return (
              <path key={`${from}-${to}`}
                d={`M ${f.x - nodeR * 0.7} ${f.y + nodeR * 0.7} L ${elbowX} ${startY + 20} L ${elbowX} ${endY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
              />
            );
          }

          // Elbow edges: os_gate → log nodes (hw_mood / hw_wellbeing /
          // hw_music_suggestion) sitting BELOW tg_alert in the same column.
          // Route right out of os_gate, down past tg_alert, then left back
          // into the target so the line never overlaps tg_alert.
          if (from === "os_gate" && (to === "hw_mood" || to === "hw_wellbeing" || to === "hw_music_suggestion" || to === "hw_posture")) {
            const elbowX = f.x + 90; // offset right of source/target column
            const startX = f.x + nodeR + 4;
            const startY = f.y;
            const endX = t.x + nodeR + 4; // enter from right side
            const endY = t.y;
            return (
              <path key={`${from}-${to}`}
                d={`M ${startX} ${startY} L ${elbowX} ${startY} L ${elbowX} ${endY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
                strokeDasharray="6 4"
              />
            );
          }

          // Elbow L edge: tg_alert → tg_out (go right then up)
          if (from === "tg_alert" && to === "tg_out") {
            const startX = f.x + nodeR + 4;
            const startY = f.y;
            const endX = t.x;
            const endY = t.y + nodeR + 4;
            const active = (visitedStages.has(from) || from === activeStage) && (visitedStages.has(to) || to === activeStage);
            return (
              <path key={`${from}-${to}`}
                d={`M ${startX} ${startY} L ${endX} ${startY} L ${endX} ${endY}`}
                stroke={color} strokeWidth={sw} fill="none"
                markerEnd={marker} opacity={op}
                strokeDasharray={active ? undefined : "6 4"}
              />
            );
          }

          const isGateEdge = from === "os_gate" || to === "os_gate";
          // HW marker path: agent_response fires inline markers — shown as dashed to distinguish from LLM tool path
          const isHWMarkerEdge = from === "agent_response" && (to === "hw_emotion" || to === "hw_led" || to === "hw_servo" || to === "hw_audio" || to === "hw_wellbeing" || to === "hw_mood" || to === "hw_music_suggestion" || to === "hw_posture");

          // tool_exec is rendered as a rect (the Event Pipeline), not a
          // circle. Anchor any edge that touches it on the rect boundary
          // closest to the OTHER endpoint, so arrows latch precisely to
          // the rect edge instead of pointing at a floating interior
          // pixel.
          let x1: number, y1: number, x2: number, y2: number;
          if (from === "tool_exec" && to !== "tool_exec") {
            const a = pipeAnchor(t.x, t.y);
            const dxA = t.x - a.x, dyA = t.y - a.y;
            const lenA = Math.sqrt(dxA * dxA + dyA * dyA) || 1;
            x1 = a.x;
            y1 = a.y;
            x2 = t.x - (dxA / lenA) * (nodeR + 4);
            y2 = t.y - (dyA / lenA) * (nodeR + 4);
          } else if (to === "tool_exec" && from !== "tool_exec") {
            const a = pipeAnchor(f.x, f.y);
            const dxA = a.x - f.x, dyA = a.y - f.y;
            const lenA = Math.sqrt(dxA * dxA + dyA * dyA) || 1;
            x1 = f.x + (dxA / lenA) * nodeR;
            y1 = f.y + (dyA / lenA) * nodeR;
            x2 = a.x;
            y2 = a.y;
          } else {
            const dx = t.x - f.x, dy = t.y - f.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            x1 = f.x + (dx / len) * nodeR;
            y1 = f.y + (dy / len) * nodeR;
            x2 = t.x - (dx / len) * (nodeR + 4);
            y2 = t.y - (dy / len) * (nodeR + 4);
          }
          const dashArray = isGateEdge || isHWMarkerEdge ? "6 4" : undefined;
          return (
            <line key={`${from}-${to}`} x1={x1} y1={y1} x2={x2} y2={y2}
              stroke={color} strokeWidth={sw}
              markerEnd={marker} opacity={op}
              {...(dashArray ? { strokeDasharray: dashArray } : {})}
            />
          );
        })}

        {/* Event Pipeline — replaces the old llm_first_token / agent_thinking /
            tool_exec nodes. Lists OpenClaw stream events in chronological order
            with consecutive same-type deltas merged into one row. */}
        {(() => {
          const px = PIPE.x, py = PIPE.y, pw = PIPE.w, ph = PIPE.h;
          const pipelineRows = aggregateEvents(turnEvents);
          // Pipeline is always visible — it's the canonical visual anchor for
          // the agent core. When the turn is local-match / idle / dropped, the
          // rows list shows the "(no agent stream events ...)" placeholder so
          // the canvas shape stays consistent across turn types.
          const pipelineColor = "var(--lm-blue)";
          const fmtDur = (ms: number) => ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
            : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
          const fmtChars = (n: number) => n >= 1000 ? `~${(n / 1000).toFixed(1)}k chars` : `${n} chars`;
          const fmtClockMs = (ms: number): string => {
            const d = new Date(ms);
            const pad = (n: number, w = 2) => String(n).padStart(w, "0");
            return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
          };
          // Header summary: openclaw init / llm work / tool exec / writing tail.
          // Pulled from turnEvents (chat_send, lifecycle_*) + pipelineRows
          // (tool durations).
          let chatSendTs = 0, lcStartTs = 0, lcEndTs = 0;
          for (const ev of turnEvents) {
            const tt = new Date(ev.time).getTime();
            const fn = (ev.detail as Record<string, any> | undefined)?.node;
            if ((ev.type === "chat_send" || fn === "chat_send") && !chatSendTs) chatSendTs = tt;
            if (fn === "lifecycle_start" && !lcStartTs) lcStartTs = tt;
            if (fn === "lifecycle_end") lcEndTs = tt;
          }
          const toolTotalMs = pipelineRows
            .filter((r) => r.kind === "tool" && r.durationMs > 0)
            .reduce((acc, r) => acc + r.durationMs, 0);
          const initMs = (chatSendTs && lcStartTs && lcStartTs > chatSendTs) ? lcStartTs - chatSendTs : 0;
          const turnLlmMs = (lcStartTs && lcEndTs && lcEndTs > lcStartTs) ? (lcEndTs - lcStartTs) - toolTotalMs : 0;
          const totalMs = (chatSendTs && lcEndTs) ? lcEndTs - chatSendTs : 0;
          // Sum components are joined with "·" and the total is glued on
          // with " = " so the line reads as a literal sum:
          //   init 2.8s · llm 15.1s · tool 0.5s = total 18.4s
          const sumParts: string[] = [];
          if (initMs > 0) sumParts.push(`init ${fmtDur(initMs)}`);
          if (turnLlmMs > 0) sumParts.push(`llm ${fmtDur(turnLlmMs)}`);
          if (toolTotalMs > 0) sumParts.push(`tool ${fmtDur(toolTotalMs)}`);
          const headerSummary = sumParts.length > 0 && totalMs > 0
            ? `${sumParts.join("  ·  ")}  =  total ${fmtDur(totalMs)}`
            : sumParts.length > 0
            ? sumParts.join("  ·  ")
            : (totalMs > 0 ? `total ${fmtDur(totalMs)}` : "");
          const rowColor = (kind: string) => {
            if (kind === "thinking" || kind === "thinking_first_token") return "var(--lm-purple)";
            if (kind === "assistant" || kind === "agent_first_token") return "var(--lm-blue)";
            if (kind === "tool" || kind === "tool_result") return "var(--lm-amber)";
            if (kind === "lifecycle_start" || kind === "lifecycle_end") return "var(--lm-green)";
            if (kind === "error") return "var(--lm-red)";
            if (kind === "compaction") return "var(--lm-purple)";
            return "var(--lm-text-muted)";
          };
          const guideBtnX = px + pw - 14;
          const guideBtnY = py + 10;
          const copyBtnX = px + pw - 28;
          const copyBtnY = py + 10;
          // Plain-text dump of the pipeline content for the clipboard.
          // Same shape as what the user reads on screen, easy to paste into
          // a bug report or log.
          const buildPipelineText = (): string => {
            const lines: string[] = [];
            lines.push(`⟨agent event pipeline⟩`);
            if (headerSummary) lines.push(`⏱ ${headerSummary}`);
            for (let i = 0; i < pipelineRows.length; i++) {
              const r = pipelineRows[i];
              // Real wall-clock stamp of the first event in this row, so the
              // copied text can be correlated against device logs.
              let line = `${fmtClockMs(r.startMs)}  ${r.label}`;
              if (r.kind === "thinking" || r.kind === "assistant") {
                line += `  ${fmtDur(r.durationMs)} · ${r.chunks} chunks · ${fmtChars(r.chars)}`;
              } else if (r.kind === "tool") {
                line += `  ${r.durationMs > 0 ? fmtDur(r.durationMs) : "…"}`;
                if (r.detail) line += `  ${r.detail}`;
              } else if (r.detail) {
                line += `  ${r.detail}`;
              }
              lines.push(line);
              const next = pipelineRows[i + 1];
              const gapMs = next ? next.startMs - r.endMs : 0;
              if (gapMs > 200) {
                lines.push(`    ⋯ + ${fmtDur(gapMs)} OS Server waiting next event`);
              }
            }
            return lines.join("\n");
          };
          const handleCopyPipeline = () => {
            const text = buildPipelineText();
            const done = () => {
              setPipelineCopied(true);
              window.setTimeout(() => setPipelineCopied(false), 1200);
            };
            // navigator.clipboard is undefined on non-secure origins (http://Pi).
            // Fall back to a hidden textarea + execCommand so the button still
            // works when the monitor is served over plain HTTP from the device.
            if (navigator.clipboard && window.isSecureContext) {
              navigator.clipboard.writeText(text).then(done).catch(() => {
                fallbackCopy(text);
                done();
              });
              return;
            }
            fallbackCopy(text);
            done();
          };
          const guideEntries: { stream: string; desc: string; common: boolean }[] = [
            { stream: "lifecycle:start", desc: "Turn begins. Agent acked chat.send and is about to call the LLM.", common: true },
            { stream: "thinking",            desc: "LLM reasoning delta. Codex thinking=low / Claude extended thinking. Many per turn.", common: true },
            { stream: "thinking:first_token",desc: "Marker — first delta of the thinking stream. Persisted to JSONL so reloaded turns show when reasoning began. Only fires when extended thinking is enabled.", common: false },
            { stream: "assistant",           desc: "LLM reply text delta. The string that becomes the assistant message / TTS.", common: true },
            { stream: "agent:first_token",   desc: "Marker — first text delta of the assistant reply (not first tool call). Persisted to JSONL so reloaded turns show when text streaming began. Tool-only turns (NO_REPLY) won't fire this.", common: true },
            { stream: "tool · start",    desc: "Tool function call started. Carries the tool name + args.", common: true },
            { stream: "tool · result",   desc: "Tool returned. OS Server attaches duration to the tool row.", common: true },
            { stream: "lifecycle:end",   desc: "Turn complete. Includes optional usage tokens. OS Server flushes TTS here.", common: true },
            { stream: "error",           desc: "Turn errored mid-run (network, model, quota). Rare — investigate.", common: false },
            { stream: "compaction",      desc: "Auto-compact in progress. Session history is being summarized.", common: false },
            { stream: "item",            desc: "Codex CLI internal: each reasoning / tool / message wrapped as an item.", common: false },
            { stream: "plan",            desc: "Codex CLI planning step. Not used by OS Server.", common: false },
            { stream: "approval",        desc: "Codex CLI approval prompt. Not used by OS Server.", common: false },
            { stream: "command_output",  desc: "Bash tool stdout streaming. Fires for shell commands.", common: false },
            { stream: "patch",           desc: "Codex CLI file patch. Not used by OS Server.", common: false },
          ];
          return (
            <g>
              <rect x={px} y={py} width={pw} height={ph} rx={10}
                fill="var(--lm-card)" fillOpacity={0.55}
                stroke={pipelineColor} strokeOpacity={0.5} strokeWidth={1.5}
                strokeDasharray="3 2"
              />
              <text x={px + 8} y={py + 12}
                fill={pipelineColor} fontSize={7} fontWeight={700}
                fontFamily="monospace" opacity={0.85} style={{ letterSpacing: "0.06em" }}>
                ⟨agent event pipeline⟩
              </text>
              {/* Copy button: just left of the guide ?. Copies the pipeline
                  content as plain text to the clipboard. */}
              <g
                onMouseDown={(e: React.MouseEvent) => { e.stopPropagation(); }}
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); handleCopyPipeline(); }}
                style={{ cursor: "pointer" }}
              >
                <circle cx={copyBtnX} cy={copyBtnY} r={5}
                  fill="var(--lm-card)" fillOpacity={0.7}
                  stroke={pipelineColor} strokeWidth={1} strokeOpacity={0.8}
                />
                <text x={copyBtnX} y={copyBtnY + 1.8} textAnchor="middle"
                  fontSize={5.5} fontWeight={700} fontFamily="monospace"
                  fill={pipelineColor}
                  style={{ pointerEvents: "none" }}>
                  {pipelineCopied ? "✓" : "⎘"}
                </text>
              </g>
              {/* Guide button: top-right corner. Click toggles a popup
                  listing the OpenClaw stream types this pipeline can show. */}
              <g
                onMouseDown={(e: React.MouseEvent) => { e.stopPropagation(); }}
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); setPipelineGuideOpen(v => !v); }}
                style={{ cursor: "pointer" }}
              >
                <circle cx={guideBtnX} cy={guideBtnY} r={5}
                  fill={pipelineGuideOpen ? pipelineColor : "var(--lm-card)"}
                  fillOpacity={pipelineGuideOpen ? 0.9 : 0.7}
                  stroke={pipelineColor} strokeWidth={1} strokeOpacity={0.8}
                />
                <text x={guideBtnX} y={guideBtnY + 1.8} textAnchor="middle"
                  fontSize={6.5} fontWeight={700} fontFamily="monospace"
                  fill={pipelineGuideOpen ? "var(--lm-card)" : pipelineColor}
                  style={{ pointerEvents: "none" }}>
                  ?
                </text>
              </g>
              <foreignObject x={px + 6} y={py + 18} width={pw - 12} height={ph - 24} overflow="visible">
                <div
                  // @ts-expect-error xmlns required for foreignObject HTML
                  xmlns="http://www.w3.org/1999/xhtml"
                  onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                  style={{
                    fontFamily: "monospace",
                    fontSize: 6.5,
                    lineHeight: 1.55,
                    color: "var(--lm-text)",
                    overflow: "auto",
                    maxHeight: ph - 24,
                    userSelect: "text",
                    WebkitUserSelect: "text",
                  }}
                >
                  {/* Header summary: openclaw init / llm / tool / total.
                      All timing text is purple (var(--lm-purple)) so any
                      number-with-a-time-unit anywhere in the pipeline reads
                      as the same conceptual category. */}
                  {headerSummary && (
                    <div style={{
                      display: "flex", gap: 8, marginBottom: 4, padding: "2px 4px",
                      fontSize: 6, opacity: 0.95,
                      color: "var(--lm-purple)",
                      borderBottom: "1px dashed color-mix(in srgb, var(--lm-blue) 40%, transparent)",
                      paddingBottom: 3,
                    }}>
                      <span style={{ color: "var(--lm-purple)", display: "inline-flex", alignItems: "center" }}>
                        <Timer size={8} strokeWidth={2.5} />
                      </span>
                      <span>{headerSummary}</span>
                    </div>
                  )}
                  {pipelineRows.length === 0 ? (
                    <div style={{ opacity: 0.6, padding: "6px 4px", color: "var(--lm-text)" }}>
                      (no agent stream events captured for this turn)
                    </div>
                  ) : pipelineRows.map((r, i) => {
                    const c = rowColor(r.kind);
                    const isStream = r.kind === "thinking" || r.kind === "assistant";
                    const isOneShot = r.kind === "lifecycle_start" || r.kind === "lifecycle_end"
                      || r.kind === "agent_first_token" || r.kind === "thinking_first_token"
                      || r.kind === "compaction" || r.kind === "error";
                    // Gap to NEXT row — rendered below this row when > 200ms
                    // so the user sees idle time (e.g., "+ 6.3s" between
                    // lifecycle_start and the first tool call = LLM thinking
                    // before any tool was invoked).
                    const next = pipelineRows[i + 1];
                    const gapMs = next ? next.startMs - r.endMs : 0;
                    return (
                      <div key={i}>
                        <div style={{ display: "flex", gap: 4, padding: "1px 2px", borderLeft: `2px solid ${c}`, paddingLeft: 4, marginBottom: 1 }}>
                          <span style={{ color: c, fontWeight: 700, minWidth: 56 }}>{r.label}</span>
                          {isStream && (
                            <span>
                              <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                                {fmtDur(r.durationMs)}
                              </span>
                              <span style={{ color: "var(--lm-text)", opacity: 0.85 }}>
                                {" "}· {r.chunks} chunks · {fmtChars(r.chars)}
                              </span>
                            </span>
                          )}
                          {r.kind === "tool" && (
                            <span style={{ minWidth: 0, flex: 1 }}>
                              <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                                {r.durationMs > 0 ? fmtDur(r.durationMs) : "…"}
                              </span>
                              {r.detail ? (
                                <span
                                  style={{
                                    color: "var(--lm-text)", opacity: 0.7, marginLeft: 6,
                                    wordBreak: "break-all" as const,
                                    whiteSpace: "pre-wrap" as const,
                                  }}
                                >
                                  {r.detail}
                                </span>
                              ) : null}
                            </span>
                          )}
                          {isOneShot && r.detail && (
                            <span style={{ color: "var(--lm-text)", opacity: 0.7 }}>{r.detail}</span>
                          )}
                        </div>
                        {gapMs > 200 && (
                          <div style={{
                            paddingLeft: 18, fontSize: 5.8, marginBottom: 1,
                            fontStyle: "italic",
                          }}>
                            <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>
                              ⋯ + {fmtDur(gapMs)}
                            </span>
                            <span style={{ color: "var(--lm-text)", opacity: 0.75 }}>
                              {" "}OS Server waiting next event
                            </span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </foreignObject>
              {/* Guide popup rendered LAST inside the pipeline group so it
                  paints on top of the event-row foreignObject (otherwise the
                  row list overlays the popup and swallows clicks on ✕). */}
              {pipelineGuideOpen && (
                <foreignObject
                  x={px + pw - 320} y={py + 22} width={320} height={ph - 30}
                  overflow="visible"
                  style={{ pointerEvents: "auto" }}
                >
                  <div
                    // @ts-expect-error xmlns required for foreignObject HTML
                    xmlns="http://www.w3.org/1999/xhtml"
                    onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                    onClick={(e: React.MouseEvent) => e.stopPropagation()}
                    style={{
                      background: "#34D399",
                      border: "1px solid #0F766E",
                      borderRadius: 6,
                      padding: 8,
                      fontFamily: "monospace",
                      fontSize: 7,
                      lineHeight: 1.5,
                      color: "#0A2A1F",
                      maxHeight: ph - 30,
                      overflow: "auto",
                      boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
                      pointerEvents: "auto",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6, alignItems: "center" }}>
                      <span style={{ fontWeight: 700, color: "#0A2A1F", fontSize: 7.5 }}>
                        Agent streams (event:"agent")
                      </span>
                      <button
                        type="button"
                        onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                        onClick={(e: React.MouseEvent) => { e.stopPropagation(); setPipelineGuideOpen(false); }}
                        style={{
                          cursor: "pointer", opacity: 0.85,
                          background: "transparent", border: "none", color: "#0A2A1F",
                          padding: "0 4px", lineHeight: 1,
                          display: "inline-flex", alignItems: "center",
                        }}
                      ><X size={11} strokeWidth={2.5} /></button>
                    </div>
                    <div style={{ opacity: 0.8, marginBottom: 5, fontSize: 6.5, color: "#0A2A1F" }}>
                      Common turn: lifecycle:start → thinking / assistant / tool — lifecycle:end. The other 7 fire only in special situations.
                    </div>
                    {guideEntries.map((e) => (
                      <div key={e.stream} style={{ marginBottom: 4, opacity: e.common ? 1 : 0.7 }}>
                        <span style={{ color: "#0A2A1F", fontWeight: 700 }}>{e.stream}</span>
                        <span style={{ marginLeft: 6, color: "#0A2A1F", opacity: e.common ? 0.9 : 0.75 }}>{e.desc}</span>
                      </div>
                    ))}
                  </div>
                </foreignObject>
              )}
            </g>
          );
        })()}

        {/* Nodes */}
        {FLOW_NODES.map((node) => {
          // Hidden agent-core anchors — their FlowStage entries remain so
          // edges and visited tracking still work, but their node circles
          // are absorbed into the Event Pipeline rect (rendered separately
          // below). Skip rendering here.
          if (node.id === "agent_thinking" || node.id === "tool_exec") {
            return null;
          }
          const pos = positions[node.id];
          const isActive = node.id === activeStage;
          const isVisited = visitedStages.has(node.id);
          const color = nodeColor(node.id);
          const opacity = nodeOpacity(node.id);
          const lines = nodeInfo[node.id] ?? [];
          const hasInfo = lines.length > 0 && (isActive || isVisited);
          const descLines = node.desc.split(" · ").length;
          // agent_call info box renders ABOVE the node so its (often long)
          // message + token block doesn't sit on top of the Event Pipeline rect.
          const boxAbove = node.id === "agent_call";
          const boxY = boxAbove ? pos.y - nodeR - 4 : pos.y + nodeR + 14 + descLines * 10;
          return (
            <g key={node.id} opacity={opacity}>
              {/* Node shape based on node.shape */}
              {(() => {
                const shape = node.shape ?? "circle";
                const r = shape === "square" ? gateR : nodeR;
                const fOpacity = isActive ? 0.25 : isVisited ? 0.18 : 0.12;
                const sOpacity = isActive ? 1 : isVisited ? 0.7 : 0.35;
                const sWidth = isActive ? 2.5 : 1.5;
                const glow = isActive ? { filter: `url(#${glowId})` } : undefined;
                const glowR = r + 6;
                const props = { fill: color, fillOpacity: fOpacity, stroke: color, strokeWidth: sWidth, strokeOpacity: sOpacity, style: glow };
                const glowProps = { fill: "none" as const, stroke: color, strokeWidth: 2, opacity: 0.35, style: { filter: `url(#${glowId})` } };

                const hexPoints = (cx: number, cy: number, rad: number) =>
                  Array.from({ length: 6 }, (_, i) => {
                    const angle = (Math.PI / 3) * i - Math.PI / 6;
                    return `${cx + rad * Math.cos(angle)},${cy + rad * Math.sin(angle)}`;
                  }).join(" ");

                const diamondPoints = (cx: number, cy: number, rad: number) =>
                  `${cx},${cy - rad} ${cx + rad},${cy} ${cx},${cy + rad} ${cx - rad},${cy}`;

                switch (shape) {
                  case "hexagon":
                    return (<>
                      {isActive && <polygon points={hexPoints(pos.x, pos.y, glowR)} {...glowProps} />}
                      <polygon points={hexPoints(pos.x, pos.y, r)} {...props} />
                    </>);
                  case "diamond":
                    return (<>
                      {isActive && <polygon points={diamondPoints(pos.x, pos.y, glowR)} {...glowProps} />}
                      <polygon points={diamondPoints(pos.x, pos.y, r)} {...props} />
                    </>);
                  case "square":
                    return (<>
                      {isActive && <rect x={pos.x - glowR} y={pos.y - glowR} width={glowR * 2} height={glowR * 2} rx={12} {...glowProps} />}
                      <rect x={pos.x - r} y={pos.y - r} width={r * 2} height={r * 2} rx={10} {...props} />
                    </>);
                  default:
                    return (<>
                      {isActive && <circle cx={pos.x} cy={pos.y} r={glowR} {...glowProps} />}
                      <circle cx={pos.x} cy={pos.y} r={r} {...props} />
                    </>);
                }
              })()}
              <text x={pos.x} y={pos.y - 6} textAnchor="middle"
                fill={color} fontSize={9} fontWeight={isActive ? 700 : 600}>
                {node.id === "agent_response" && lines.some((l) => l.includes("no reply")) ? "🚫"
                  : node.id === "agent_response" && lines.some((l) => l.includes("no output")) ? "💤"
                  : node.id === "agent_response" && lines.some((l) => l.startsWith('"')) ? "💬"
                  : node.icon} {node.short}
              </text>
              <text x={pos.x} y={pos.y + 6} textAnchor="middle"
                fill={color} fontSize={7} opacity={0.9}>
                {node.label}
              </text>
              {node.desc.split(" · ").map((part, i) => (
                <text key={`d${i}`} x={pos.x} y={pos.y + nodeR + 14 + i * 10} textAnchor="middle"
                  fill={color} fontSize={5.5} opacity={0.6}>
                  {part}
                </text>
              ))}

              {hasInfo && (() => {
                const textLines = lines.filter((l) => !l.startsWith("🖼"));
                // agent_call carries the full chat_send message (often the
                // pre-injected context for emotion.detected /
                // speech_emotion.detected / motion.activity Phase 2 — several
                // KB of JSON). Widen its box and anchor the
                // left edge at the original centered position so it grows
                // rightward into the empty space toward channel_input.
                const isWide = node.id === "agent_call";
                const boxW = isWide ? 480 : 190;
                const halfDefault = 95; // = original 190 / 2 — keep left edge stable
                const xCentered = isWide ? pos.x - halfDefault : pos.x - boxW / 2;
                // agent_call box anchors at the right side of the node and
                // flows rightward (toward channel_input) so the long message
                // / token block reads left-aligned without sweeping over the
                // pipeline rect on the left.
                const boxRight = node.id === "agent_call";
                const boxX = boxRight
                  ? pos.x + nodeR + 6
                  : (boxAbove ? pos.x + nodeR - boxW : xCentered);
                return (
                  <foreignObject
                    x={boxX} y={boxY - 2}
                    width={boxW} height={1}
                    overflow="visible"
                  >
                    <div
                      // @ts-expect-error xmlns required for foreignObject HTML
                      xmlns="http://www.w3.org/1999/xhtml"
                      onMouseDown={(e: React.MouseEvent) => e.stopPropagation()}
                      style={{
                        background: "color-mix(in srgb, var(--lm-card) 70%, transparent)",
                        border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
                        borderRadius: 4,
                        padding: "4px 6px",
                        fontFamily: "monospace",
                        fontSize: 5.5,
                        lineHeight: 1.7,
                        color: color,
                        opacity: 0.95,
                        ...(boxAbove ? { transform: "translateY(-100%)" } : {}),
                        userSelect: "text",
                        WebkitUserSelect: "text",
                        cursor: "text",
                        wordBreak: "break-all" as const,
                        whiteSpace: "pre-wrap" as const,
                        maxWidth: boxW,
                      }}
                    >
                      {textLines.map((line, i) => (
                        <div key={i} style={{
                          color: line.startsWith("⏱") ? "var(--lm-amber)" : color,
                          fontWeight: line.startsWith("⏱") ? 700 : 400,
                        }}>
                          {line}
                        </div>
                      ))}
                    </div>
                  </foreignObject>
                );
              })()}
            </g>
          );
        })}

        {/* Snapshot images — below CAM node (always shown) */}
        {snapshotUrls.length > 0 && snapshotUrls.map((url, i) => {
          const imgW = 100;
          const imgH = 75;
          const snapX = 80 + i * (imgW + 10);
          const snapY = 340;
          return (
            <g key={i}>
              <rect
                x={snapX - imgW / 2} y={snapY - imgH / 2}
                width={imgW} height={imgH}
                rx={6} ry={6}
                fill="var(--lm-card)" stroke="var(--lm-amber)" strokeWidth={1}
                opacity={0.9}
              />
              <image
                href={url}
                x={snapX - imgW / 2 + 2} y={snapY - imgH / 2 + 2}
                width={imgW - 4} height={imgH - 4}
                preserveAspectRatio="xMidYMid meet"
                clipPath={`inset(0 round 4px)`}
                style={{ cursor: "pointer" }}
                onClick={(e) => { e.stopPropagation(); setLightboxUrl(url); }}
              />
              {i === 0 && (
                <text
                  x={snapX} y={snapY + imgH / 2 + 10}
                  textAnchor="middle"
                  fill="var(--lm-amber)" fontSize={6} fontWeight={600}
                >
                  📷 {snapshotUrls.length > 1 ? `${snapshotUrls.length} Snapshots` : "Snapshot"}
                </text>
              )}
            </g>
          );
        })}

        {/* Snapshot on INTENT→AGENT line — only when image was actually sent to agent */}
        {imageSentToAgent && snapshotUrls.length > 0 && snapshotUrls.slice(0, 1).map((url, i) => {
          const imgW = 80;
          const imgH = 60;
          const snapX = 515;
          const snapY = 145;
          return (
            <g key={`agent-snap-${i}`}>
              <rect
                x={snapX - imgW / 2} y={snapY - imgH / 2}
                width={imgW} height={imgH}
                rx={6} ry={6}
                fill="var(--lm-card)" stroke="var(--lm-blue)" strokeWidth={1}
                opacity={0.9}
              />
              <image
                href={url}
                x={snapX - imgW / 2 + 2} y={snapY - imgH / 2 + 2}
                width={imgW - 4} height={imgH - 4}
                preserveAspectRatio="xMidYMid meet"
                clipPath={`inset(0 round 4px)`}
                style={{ cursor: "pointer" }}
                onClick={(e) => { e.stopPropagation(); setLightboxUrl(url); }}
              />
              <text
                x={snapX} y={snapY + imgH / 2 + 10}
                textAnchor="middle"
                fill="var(--lm-blue)" fontSize={6} fontWeight={600}
              >
                📷 → Agent
              </text>
            </g>
          );
        })}
      </svg>

      {/* Snapshot lightbox — portalled to <body> so position:fixed anchors to
          the viewport. The diagram lives inside transformed/animated ancestors
          (zoom/pan + the turn card), any of which would otherwise become the
          fixed containing block and let the lightbox overflow on top of the
          page rather than cover it. `lm-root ${themeClass}` re-scopes tokens. */}
      {lightboxUrl && createPortal(
        <div
          className={`lm-root ${themeClass}`}
          onClick={() => setLightboxUrl(null)}
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: "fixed", inset: 0, zIndex: 9999,
            background: "rgba(0,0,0,0.8)", backdropFilter: "blur(4px)",
            display: "flex", alignItems: "center", justifyContent: "center",
            cursor: "pointer",
          }}
        >
          <button
            onClick={() => setLightboxUrl(null)}
            aria-label="Close"
            style={{
              position: "absolute", top: 16, right: 16,
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", width: 36, height: 36,
              borderRadius: "50%", cursor: "pointer",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <X size={20} strokeWidth={2.25} />
          </button>
          <img
            src={lightboxUrl}
            onClick={(e) => e.stopPropagation()}
            style={{ width: "85vw", height: "85vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
          />
        </div>,
        document.body,
      )}

      {/* Shape legend */}
      <div style={{
        display: "flex", gap: 16, justifyContent: "center", alignItems: "center",
        fontSize: 10, color: "var(--lm-text-muted)", padding: "8px 0 4px",
      }}>
        {([
          { label: "Input", color: "var(--lm-amber)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <polygon points={Array.from({ length: 6 }, (_, i) => {
                const a = (Math.PI / 3) * i - Math.PI / 6;
                return `${6 * Math.cos(a)},${6 * Math.sin(a)}`;
              }).join(" ")} fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Process", color: "var(--lm-blue)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <circle r="6" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Output", color: "var(--lm-purple)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <polygon points="0,-7 7,0 0,7 -7,0" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
          { label: "Gate", color: "var(--lm-teal)", shape: (c: string) => (
            <svg width="16" height="16" viewBox="-8 -8 16 16" style={{ verticalAlign: "middle" }}>
              <rect x="-5.5" y="-5.5" width="11" height="11" rx="2.5" fill={c} fillOpacity={0.2} stroke={c} strokeWidth="1.5" />
            </svg>
          )},
        ] as const).map((item) => (
          <span key={item.label} style={{ display: "inline-flex", alignItems: "center", gap: 4, color: item.color }}>
            {item.shape(item.color)} {item.label}
          </span>
        ))}
      </div>

      {/* Zoom controls overlay */}
      <div style={{
        position: "absolute", bottom: 6, right: 6,
        display: "flex", gap: 4, alignItems: "center",
      }}>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)", marginRight: 4 }}>
          {Math.round(zoom * 100)}%
        </span>
        {[
          { label: "−", action: () => setZoom((z) => Math.max(0.4, z - 0.2)) },
          { label: "⟳", action: resetView },
          { label: "+", action: () => setZoom((z) => Math.min(4, z + 0.2)) },
        ].map((btn) => (
          <button key={btn.label} onClick={btn.action} className="lm-u-btn" style={{
            width: 22, height: 22, borderRadius: 5,
            color: "var(--lm-text-dim)", fontSize: 12, lineHeight: 1, padding: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>{btn.label}</button>
        ))}
      </div>
    </div>
  );
}
