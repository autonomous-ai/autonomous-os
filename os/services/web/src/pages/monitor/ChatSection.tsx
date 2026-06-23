import { useEffect, useRef, useState, useCallback, type ReactNode } from "react";
import {
  Paperclip, X, Copy, Check, RotateCcw, Download, ArrowDown,
  Pin, ChevronRight, Sparkles, Plus, Trash2, History,
  Wrench, Lightbulb, Cog, Music, Palette, Search, Smile, ChevronDown,
} from "lucide-react";
import { API } from "./types";
import { getDeviceConfig } from "@/lib/api";
import type { DisplayEvent, MonitorEvent } from "./types";

// ─── Markdown ───────────────────────────────────────────────────────────────

// Inline: **bold**, *italic*, ~~strikethrough~~, `code`, URLs
function renderInline(line: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`|(https?:\/\/[^\s<>)"]+))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(line)) !== null) {
    if (match.index > last) parts.push(line.slice(last, match.index));
    const k = `${keyPrefix}-${match.index}`;
    if (match[2]) parts.push(<strong key={k}>{match[2]}</strong>);
    else if (match[3]) parts.push(<em key={k}>{match[3]}</em>);
    else if (match[4]) parts.push(<del key={k} style={{ opacity: 0.6 }}>{match[4]}</del>);
    else if (match[5]) parts.push(<code key={k} style={{ background: "var(--lm-surface)", padding: "1px 5px", borderRadius: 3, fontSize: "0.9em" }}>{match[5]}</code>);
    else if (match[6]) parts.push(<a key={k} href={match[6]} target="_blank" rel="noopener noreferrer" style={{ color: "var(--lm-teal)", textDecoration: "underline" }}>{match[6].length > 50 ? match[6].slice(0, 50) + "…" : match[6]}</a>);
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}

function renderMarkdown(text: string): ReactNode {
  const lines = text.split("\n");
  const result: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    // Code block: ```
    if (lines[i].startsWith("```")) {
      const codeLines: string[] = [];
      i++; // skip opening ```
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++; // skip closing ```
      result.push(
        <pre key={`cb-${i}`} style={{
          background: "var(--lm-bg)", padding: "8px 12px", borderRadius: 6,
          fontSize: "0.85em", overflowX: "auto", margin: "4px 0",
          border: "1px solid var(--lm-border)", whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Headings: # ## ### #### ##### ######
    const headingMatch = lines[i].match(/^(#{1,6})\s+(.+)/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const sizes = [0, "1.3em", "1.15em", "1.05em", "1em", "0.95em", "0.9em"];
      result.push(
        <div key={`h-${i}`} style={{
          fontSize: sizes[level], fontWeight: 600,
          margin: "6px 0 2px", lineHeight: 1.3,
        }}>
          {renderInline(headingMatch[2], `h-${i}`)}
        </div>,
      );
      i++;
      continue;
    }

    // Horizontal rule: --- or *** or ___
    if (/^([-*_])\1{2,}\s*$/.test(lines[i])) {
      result.push(<hr key={`hr-${i}`} style={{ border: "none", borderTop: "1px solid var(--lm-border)", margin: "8px 0" }} />);
      i++;
      continue;
    }

    // Blockquote: > text
    if (lines[i].startsWith("> ")) {
      const quoteLines: ReactNode[] = [];
      while (i < lines.length && lines[i].startsWith("> ")) {
        quoteLines.push(
          <div key={`bq-${i}`}>{renderInline(lines[i].slice(2), `bq-${i}`)}</div>,
        );
        i++;
      }
      result.push(
        <div key={`blockquote-${i}`} style={{
          borderLeft: "3px solid rgba(245,158,11,0.4)", paddingLeft: 10,
          margin: "4px 0", color: "var(--lm-text-muted)", fontStyle: "italic",
        }}>
          {quoteLines}
        </div>,
      );
      continue;
    }

    // Table: | col | col | with separator row | --- | --- |
    if (lines[i].includes("|") && i + 1 < lines.length && /^\|?\s*[-:]+[-| :]*$/.test(lines[i + 1])) {
      const headerCells = lines[i].split("|").map((c) => c.trim()).filter(Boolean);
      i += 2; // skip header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|")) {
        rows.push(lines[i].split("|").map((c) => c.trim()).filter(Boolean));
        i++;
      }
      result.push(
        <div key={`tw-${i}`} style={{ overflowX: "auto", margin: "4px 0" }}>
          <table style={{
            borderCollapse: "collapse", fontSize: "0.9em", width: "100%",
          }}>
            <thead>
              <tr>
                {headerCells.map((h, ci) => (
                  <th key={ci} style={{
                    padding: "4px 8px", borderBottom: "1px solid var(--lm-border)",
                    textAlign: "left", fontWeight: 600, fontSize: "0.9em",
                  }}>{renderInline(h, `th-${i}-${ci}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri}>
                  {row.map((cell, ci) => (
                    <td key={ci} style={{
                      padding: "3px 8px", borderBottom: "1px solid var(--lm-border)",
                    }}>{renderInline(cell, `td-${i}-${ri}-${ci}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Unordered list: - item or * item
    if (/^[\-\*]\s/.test(lines[i])) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^[\-\*]\s/.test(lines[i])) {
        items.push(<li key={`li-${i}`}>{renderInline(lines[i].replace(/^[\-\*]\s/, ""), `ul-${i}`)}</li>);
        i++;
      }
      result.push(<ul key={`ul-${i}`} style={{ margin: "4px 0", paddingLeft: 20 }}>{items}</ul>);
      continue;
    }

    // Ordered list: 1. item
    if (/^\d+\.\s/.test(lines[i])) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(<li key={`oli-${i}`}>{renderInline(lines[i].replace(/^\d+\.\s/, ""), `ol-${i}`)}</li>);
        i++;
      }
      result.push(<ol key={`ol-${i}`} style={{ margin: "4px 0", paddingLeft: 20 }}>{items}</ol>);
      continue;
    }

    // Regular line
    const inline = renderInline(lines[i], `l-${i}`);
    result.push(
      <span key={`s-${i}`}>
        {i > 0 && result.length > 0 && <br />}
        {inline.length > 0 ? inline : ""}
      </span>,
    );
    i++;
  }

  return result;
}

// Strip inline HW control markers like [HW:/emotion:{"emotion":"curious","intensity":0.7}]
function stripHWMarkers(text: string): string {
  return text.replace(/\[HW:\/[^\]]*\]/g, "").trim();
}

// ─── Tool call parsing ──────────────────────────────────────────────────────

type IconKind = "tool" | "led" | "scene" | "led_off" | "music" | "servo" | "emotion" | "search";
interface ToolChip {
  id: string;       // dedup key
  iconKind: IconKind;
  label: string;
  detail?: string;  // formatted arg summary, e.g. for web_search → query
  args?: Record<string, any>; // raw args for expanded view
  result?: string;  // result summary (when "result" phase arrives)
}

const TOOL_EVENT_TYPES = new Set(["tool_call", "hw_emotion", "hw_led", "hw_audio", "hw_servo", "led_set", "led_off"]);

// Tools that route through hw_* events already — skip the generic tool_call
// chip for them to avoid duplicates.
const HW_SHADOW_TOOLS = new Set(["set_emotion", "set_led", "play_music", "move_servo"]);

// Map a tool name to which lucide icon best represents it. web_search and
// similar lookup tools get a search icon, the rest fall back to Wrench.
function iconForTool(name: string): IconKind {
  const n = name.toLowerCase();
  if (n.includes("search") || n.includes("lookup") || n.includes("query")) return "search";
  return "tool";
}

// Render a tool chip's icon as a Lucide component at the given size.
function renderToolIcon(kind: IconKind, size = 12) {
  switch (kind) {
    case "led":     return <Lightbulb size={size} />;
    case "scene":   return <Palette size={size} />;
    case "led_off": return <Lightbulb size={size} />;
    case "music":   return <Music size={size} />;
    case "servo":   return <Cog size={size} />;
    case "emotion": return <Smile size={size} />;
    case "search":  return <Search size={size} />;
    case "tool":
    default:        return <Wrench size={size} />;
  }
}

// Compact one-line preview of args — typically the user-relevant input like
// the search query. Falls back to a JSON-ish stringification.
function summarizeArgs(args: Record<string, any> | undefined): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  // Common keys we prefer to surface as the "headline" of the chip.
  for (const key of ["query", "q", "url", "command", "text", "name", "recording"]) {
    if (typeof args[key] === "string" && args[key]) {
      const v = args[key];
      return v.length > 80 ? v.slice(0, 80) + "…" : v;
    }
  }
  try {
    const j = JSON.stringify(args);
    return j.length > 80 ? j.slice(0, 80) + "…" : j;
  } catch {
    return undefined;
  }
}

interface ToolEventInput {
  type: string;
  summary: string;
  id: string;
  detail?: Record<string, any> | null;
  phase?: string;
}

// parseToolChip turns a flow event into a chip. For `tool_call`:
// - start phase carries the args in `detail.args` (JSON string)
// - end phase summary is `"Tool <name> done: <result up to 100 chars>"` —
//   we extract the result tail so users see what the tool actually returned.
function parseToolChip(ev: ToolEventInput): ToolChip | null {
  const s = ev.summary;
  switch (ev.type) {
    case "hw_emotion": {
      const m = s.match(/"emotion"\s*:\s*"([^"]+)"/);
      return { id: ev.id, iconKind: "emotion", label: m ? m[1] : "emotion" };
    }
    case "hw_led": {
      if (s.includes("/scene/")) {
        const m = s.match(/\/scene\/(\w+)/);
        return { id: ev.id, iconKind: "scene", label: m ? `scene: ${m[1]}` : "LED scene" };
      }
      if (s.includes("/led/off")) return { id: ev.id, iconKind: "led_off", label: "LED off" };
      const m = s.match(/"hex"\s*:\s*"([^"]+)"/);
      return { id: ev.id, iconKind: "led", label: m ? `LED ${m[1]}` : "LED" };
    }
    case "led_off": return { id: ev.id, iconKind: "led_off", label: "LED off" };
    case "led_set": return null;
    case "hw_audio": return { id: ev.id, iconKind: "music", label: "music" };
    case "hw_servo": {
      if (s.includes("/aim")) return { id: ev.id, iconKind: "servo", label: "servo aim" };
      if (s.includes("/play")) {
        const m = s.match(/\/play\/(\w+)/);
        return { id: ev.id, iconKind: "servo", label: m ? `servo: ${m[1]}` : "servo play" };
      }
      return { id: ev.id, iconKind: "servo", label: "servo" };
    }
    case "tool_call": {
      const d = ev.detail as Record<string, any> | undefined;
      // Server (handler_events.go) sets phase as a top-level event field; older
      // flow_event paths nest it under detail.data.phase — accept both.
      const phase: string = ev.phase ?? d?.data?.phase ?? d?.phase ?? "";
      const name: string =
        d?.tool ?? d?.data?.tool ?? d?.data?.name ?? d?.name
        ?? (s.match(/^(\w+)/)?.[1] ?? "tool");
      if (HW_SHADOW_TOOLS.has(name)) return null;
      let argsObj: Record<string, any> | undefined;
      const rawArgs = d?.args ?? d?.data?.args;
      if (rawArgs) {
        try {
          argsObj = typeof rawArgs === "string" ? JSON.parse(rawArgs) : rawArgs;
        } catch { /* keep undefined */ }
      }
      const isResult = phase === "result" || phase === "end";
      // Lift the truncated result text out of the summary for end-phase events.
      // Format: "Tool <name> done: <result>" — we keep what comes after ": ".
      let resultText: string | undefined;
      if (isResult) {
        const m = s.match(/done:\s*(.+)$/);
        resultText = m ? m[1] : "completed";
      }
      return {
        id: ev.id,
        iconKind: iconForTool(name),
        label: name,
        detail: summarizeArgs(argsObj),
        args: argsObj,
        result: resultText,
      };
    }
    default: return null;
  }
}

// ─── Storage ────────────────────────────────────────────────────────────────

const CONVOS_KEY = "os_chat_convos";
const ACTIVE_KEY = "os_chat_active";
const MAX_MESSAGES = 200;
const MAX_CONVOS = 50;

// Conversation history TTL — auto-purge anything older than this on next load.
// Chat content can include voice transcripts, names, schedules, mood notes —
// not the kind of data to keep indefinitely in localStorage where any
// same-origin script or browser extension can read it. 7 days is enough to
// resume a recent conversation without piling up months of history.
const HISTORY_TTL_MS = 7 * 24 * 60 * 60 * 1000;

// Storage envelope so the TTL check has a timestamp to look at. Legacy
// devices have a bare Conversation[] under CONVOS_KEY; loadConvos() handles
// both shapes and re-saves into the envelope on next save.
interface ConvosEnvelope {
  savedAt: number;
  convos: Conversation[];
}

interface ChatMessage {
  id: string;
  role: "user" | "agent";
  text: string;
  time: string;
  date?: string;       // YYYY-MM-DD for date separators
  imageUrl?: string;   // data: URL for attached images (not persisted to save space)
  fileName?: string;   // original filename for non-image files
  fileSize?: number;   // bytes
  runId?: string;
  pending?: boolean;
  error?: boolean;
  tools?: ToolChip[];  // tool calls made during this response
  tokenUsage?: { input: number; output: number; cacheRead?: number; cacheWrite?: number; total: number };
}

interface Conversation {
  id: string;
  title: string;
  createdAt: number;
  messages: ChatMessage[];
  manualTitle?: boolean;
  pinned?: boolean;
}

function loadConvos(): Conversation[] {
  try {
    const raw = localStorage.getItem(CONVOS_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw) as Conversation[] | ConvosEnvelope;

    // Legacy shape: bare Conversation[]. Treat as still-fresh (the user is
    // upgrading right now); the next saveConvos() wraps it into the envelope.
    if (Array.isArray(parsed)) {
      return parsed.map((c) => ({ ...c, messages: cleanPending(c.messages) }));
    }

    // Envelope shape: enforce TTL. Stale → drop and start clean.
    if (parsed && typeof parsed.savedAt === "number" && Array.isArray(parsed.convos)) {
      if (Date.now() - parsed.savedAt > HISTORY_TTL_MS) {
        localStorage.removeItem(CONVOS_KEY);
        localStorage.removeItem(ACTIVE_KEY);
        return [];
      }
      return parsed.convos.map((c) => ({ ...c, messages: cleanPending(c.messages) }));
    }

    return [];
  } catch {
    return [];
  }
}

function cleanPending(msgs: ChatMessage[]): ChatMessage[] {
  return msgs.map((m) =>
    m.pending
      ? { ...m, pending: false, text: m.text || "…", error: !m.text }
      : m,
  );
}

function titleFromMessages(msgs: ChatMessage[]): string {
  const userMsg = msgs.find((m) => m.role === "user");
  if (!userMsg) return "New chat";
  const agentMsg = msgs.find((m) => m.role === "agent" && !m.pending && !m.error && m.text && m.text !== "…");
  if (agentMsg) {
    const q = userMsg.text.length > 20 ? userMsg.text.slice(0, 20) + "…" : userMsg.text;
    const a = agentMsg.text.replace(/\n/g, " ");
    const aShort = a.length > 20 ? a.slice(0, 20) + "…" : a;
    return `${q} → ${aShort}`;
  }
  return userMsg.text.length > 36 ? userMsg.text.slice(0, 36) + "…" : userMsg.text;
}

function saveConvos(convos: Conversation[]) {
  try {
    const trimmed = convos.slice(0, MAX_CONVOS).map((c) => ({
      ...c,
      // Strip large data from localStorage (imageUrl data: URLs are too large)
      messages: c.messages.slice(-MAX_MESSAGES).map(({ imageUrl: _, ...m }) => m),
      // fileName/fileSize are kept — they're small strings/numbers
    }));
    const envelope: ConvosEnvelope = { savedAt: Date.now(), convos: trimmed };
    localStorage.setItem(CONVOS_KEY, JSON.stringify(envelope));
  } catch {}
}

// clearLocalChatHistory wipes the conversation cache from localStorage —
// exposed via the Clear button in the chat header so the user can drop
// stored history immediately without waiting for the TTL to fire.
function clearLocalChatHistory() {
  try {
    localStorage.removeItem(CONVOS_KEY);
    localStorage.removeItem(ACTIVE_KEY);
  } catch {}
}

function loadActiveId(): string | null {
  try { return localStorage.getItem(ACTIVE_KEY); } catch { return null; }
}

function saveActiveId(id: string | null) {
  try {
    if (id) localStorage.setItem(ACTIVE_KEY, id);
    else localStorage.removeItem(ACTIVE_KEY);
  } catch {}
}

// ─── Clipboard helper ───────────────────────────────────────────────────────

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  // Fallback for older browsers / non-HTTPS
  return new Promise((resolve) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    resolve();
  });
}

// ─── Component ──────────────────────────────────────────────────────────────

interface Props {
  events: DisplayEvent[];
  // ChatSection stays mounted (display:none) across section switches so
  // chat state and scroll position persist. isActive tells it whether
  // the user is actually viewing the Chat tab right now — when false,
  // we don't open the live /openclaw/events SSE, which otherwise would
  // hold an HTTP/1.1 connection slot from every other section.
  isActive: boolean;
}

export function ChatSection({ events, isActive }: Props) {
  const [convos, setConvos] = useState<Conversation[]>(loadConvos);
  const [activeId, setActiveId] = useState<string | null>(() => {
    const saved = loadActiveId();
    return saved && loadConvos().some((c) => c.id === saved) ? saved : null;
  });
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [search, setSearch] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  // Track hovered conversation so pin/delete buttons fade in only over the
  // active row — keeps the sidebar list visually quiet at rest.
  const [hoveredConvoId, setHoveredConvoId] = useState<string | null>(null);

  // Resolve the active model name so we can label each assistant message
  // ("claude-haiku-4-5-20251001" → "haiku-4-5"). Pulled from the sanitized
  // /api/device/config; the old `${AGENT_API}/config-json` path is now
  // loopback-only (audit local F5c) and unreachable from a browser.
  const [modelLabel, setModelLabel] = useState<string>("");
  useEffect(() => {
    getDeviceConfig()
      .then((cfg) => {
        const primary = cfg.llm_model;
        if (!primary) return;
        // Strip provider prefix and collapse Anthropic's trailing version
        // suffix so the badge stays compact.
        const raw = primary.includes("/") ? primary.split("/").pop() ?? primary : primary;
        const compact = raw
          .replace(/^claude-/i, "")
          .replace(/-\d{8}$/, "");
        setModelLabel(compact);
      })
      .catch(() => {});
  }, []);

  // Shared compact icon-button style for the per-row pin/delete actions.
  const hoverIconBtnStyle = (color: string): React.CSSProperties => ({
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    width: 22, height: 22, padding: 0, borderRadius: 4,
    background: "transparent", border: "none", cursor: "pointer",
    color,
  });

  // Shared header pill button — used by both Export and History buttons in the
  // chat top bar so the right-side toolbar is visually uniform.
  const headerPillBtnStyle: React.CSSProperties = {
    background: "var(--lm-surface)",
    border: "1px solid var(--lm-border)",
    borderRadius: 6,
    cursor: "pointer",
    color: "var(--lm-text-dim)",
    padding: "4px 10px",
    display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: 11, fontWeight: 600,
  };
  const [filePreview, setFilePreview] = useState<string | null>(null);    // data: URL (images only)
  const [fileBase64, setFileBase64] = useState<string | null>(null);      // raw base64 for API
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileSize, setFileSize] = useState<number>(0);
  const [fileIsImage, setFileIsImage] = useState(false);
  // Desktop (≥768px) always opens history by default; user can still collapse
  // for a session. Mobile (<768px) stays collapsed so the chat area gets the
  // full width. We don't persist the desktop preference — the request was that
  // history is ALWAYS open on desktop by default.
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(
    () => typeof window !== "undefined" && window.innerWidth >= 768,
  );
  const [dragging, setDragging] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingRunIdRef = useRef<string | null>(null);
  // Snapshot of the user's outgoing text for the pending run. Used to pair a
  // steered/merged turn (OpenClaw re-fires the same input under a fresh UUID
  // run) — see steered-pair branch in the events useEffect below.
  const pendingUserTextRef = useRef<string | null>(null);
  const resolvedIds = useRef<Set<string>>(new Set());
  const deltaBufRef = useRef<Map<string, string>>(new Map()); // runId → accumulated delta text
  const thinkingBufRef = useRef<Map<string, string>>(new Map()); // runId → accumulated thinking text
  const rafRef = useRef<number | null>(null); // requestAnimationFrame handle for batched rendering
  const dirtyRef = useRef(false); // whether there are pending delta/thinking updates to flush
  const [thinkingText, setThinkingText] = useState<string | null>(null); // current thinking display
  const [toolChips, setToolChips] = useState<ToolChip[]>([]); // tool calls for current pending response

  const active = convos.find((c) => c.id === activeId) ?? null;
  const messages = active?.messages ?? [];

  // Persist
  useEffect(() => { saveConvos(convos); }, [convos]);
  useEffect(() => { saveActiveId(activeId); }, [activeId]);

  // Keyboard shortcut: Cmd/Ctrl+N for new chat
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "n") {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  }, [input]);

  // Scroll detection for scroll-to-bottom button
  const onScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setShowScrollBtn(!nearBottom);
  }, []);

  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // Update messages helper
  const updateMessages = useCallback((fn: (prev: ChatMessage[]) => ChatMessage[]) => {
    setConvos((prev) =>
      prev.map((c) => {
        if (c.id !== activeId) return c;
        const updated = fn(c.messages);
        const autoTitle = !c.manualTitle ? titleFromMessages(updated) : c.title;
        return { ...c, messages: updated, title: autoTitle };
      }),
    );
  }, [activeId]);

  // Real-time monitor bus SSE — streaming deltas, thinking, tool calls
  // Uses /api/agent/events (live bus) instead of flow-stream (file-based JSONL)
  const toolChipsRef = useRef<Map<string, ToolChip>>(new Map()); // key → chip for dedup
  const tokenUsageRef = useRef<ChatMessage["tokenUsage"]>(undefined); // token usage for current run

  useEffect(() => {
    // EventSource holds one HTTP/1.1 connection slot for its lifetime.
    // ChatSection is always mounted (hidden via display:none to preserve
    // scroll/input when switching tabs), so keeping this open when the
    // whole browser tab is backgrounded burns a slot + Pi CPU for nothing.
    // Close it while hidden, reopen on visible.
    let es: EventSource | null = null;

    // Batch delta/thinking updates into a single render per animation frame
    const scheduleFlush = () => {
      if (rafRef.current != null) return; // already scheduled
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        if (!dirtyRef.current) return;
        dirtyRef.current = false;
        const p = pendingRunIdRef.current;
        if (!p) return;
        // Flush thinking
        setThinkingText(thinkingBufRef.current.get(p) ?? null);
        // Flush assistant text
        const buf = deltaBufRef.current.get(p);
        if (buf) {
          const cleaned = stripHWMarkers(buf);
          updateMessages((prev) =>
            prev.map((m) =>
              m.runId === p && m.role === "agent" && m.pending
                ? { ...m, text: cleaned }
                : m,
            ),
          );
        }
      });
    };

    const resolveRun = (runId: string) => {
      const buf = deltaBufRef.current.get(runId) ?? "";
      const text = stripHWMarkers(buf || "…");
      deltaBufRef.current.delete(runId);
      thinkingBufRef.current.delete(runId);
      resolvedIds.current.add(runId);
      pendingRunIdRef.current = null;
      pendingUserTextRef.current = null;
      setSending(false);
      setThinkingText(null);
      const chips = Array.from(toolChipsRef.current.values());
      const savedChips = chips.length > 0 ? chips : undefined;
      toolChipsRef.current.clear();
      setToolChips([]);
      const usage = tokenUsageRef.current;
      tokenUsageRef.current = undefined;
      return { text, savedChips, usage };
    };

    const onMessage = (msg: MessageEvent) => {
      try {
        const ev = JSON.parse(msg.data) as MonitorEvent;
        if (!ev.type) return;
        const pending = pendingRunIdRef.current;
        if (!pending || resolvedIds.current.has(pending)) return;

        const evRunId = ev.runId ?? (ev.detail as any)?.run_id ?? (ev.detail as any)?.runId;
        if (!evRunId || evRunId !== pending) return;

        // Tool call chips. Dedup key on iconKind+label so the start + result
        // phases of the same tool merge into a single chip — the latest event
        // wins so `result`'s completion flag overwrites the placeholder.
        //
        // Server emits tool events in two shapes:
        //   1. ev.type === "tool_call" (live monitor bus path)
        //   2. ev.type === "flow_event" with detail.node === "tool_call"
        //      (re-played from flow JSONL on reconnect)
        // We accept both — previously the chips were missed half the time
        // because only shape #1 matched, which is why chips appeared "lúc có
        // lúc không" depending on whether the user was watching live or
        // reconnected mid-turn.
        const detailNode = (ev.detail as Record<string, any> | undefined)?.node;
        const isToolCall =
          TOOL_EVENT_TYPES.has(ev.type) ||
          (ev.type === "flow_event" && detailNode && (
            detailNode === "tool_call" ||
            detailNode === "hw_emotion" ||
            detailNode === "hw_led" ||
            detailNode === "hw_audio" ||
            detailNode === "hw_servo" ||
            detailNode === "led_set" ||
            detailNode === "led_off"
          ));
        if (isToolCall) {
          // Normalize flow_event into the same shape parseToolChip expects.
          const normalizedType = TOOL_EVENT_TYPES.has(ev.type) ? ev.type : detailNode;
          const chip = parseToolChip({
            type: normalizedType,
            summary: ev.summary,
            id: ev.id,
            detail: ev.detail,
            phase: ev.phase ?? (ev.detail as any)?.data?.phase,
          });
          if (chip) {
            // Dedup key on iconKind+label+args so the start+result phases of
            // the SAME invocation merge — but two distinct invocations of the
            // same tool (e.g. two `Read` calls on different files) each get
            // their own chip. Earlier the key was just iconKind:label, which
            // collapsed all Read/Bash calls in a turn into one row.
            const argsKey = chip.detail ?? (chip.args ? JSON.stringify(chip.args) : "");
            const key = chip.iconKind + ":" + chip.label + ":" + argsKey;
            const existing = toolChipsRef.current.get(key);
            const merged: ToolChip = existing ? {
              ...existing,
              ...chip,
              args: chip.args ?? existing.args,
              detail: chip.detail ?? existing.detail,
              result: chip.result ?? existing.result,
            } : chip;
            toolChipsRef.current.set(key, merged);
            setToolChips(Array.from(toolChipsRef.current.values()));
          }
        }

        // Token usage — save for attaching to message on finalize.
        // Accept both shapes: direct `token_usage` event AND flow_event with
        // node === "token_usage" (replayed path). Also try `.data.*` nesting
        // since flow_event wraps the payload one level deeper.
        const isTokenUsage =
          ev.type === "token_usage" ||
          (ev.type === "flow_event" && (ev.detail as any)?.node === "token_usage");
        if (isTokenUsage) {
          const d = ev.detail as Record<string, any> | undefined;
          const src = (d?.data && typeof d.data === "object") ? d.data : d;
          if (src) {
            const num = (v: any) => typeof v === "number" ? v : parseInt(String(v ?? "0"), 10);
            tokenUsageRef.current = {
              input: num(src.input_tokens ?? src.input),
              output: num(src.output_tokens ?? src.output),
              cacheRead: num(src.cache_read_tokens ?? src.cache_read) || undefined,
              cacheWrite: num(src.cache_write_tokens ?? src.cache_write) || undefined,
              total: num(src.total_tokens ?? src.total),
            };
          }
          return;
        }

        // Thinking deltas — accumulate, flush on next animation frame
        if (ev.type === "thinking") {
          const delta = ev.summary ?? "";
          if (delta) {
            const buf = thinkingBufRef.current.get(pending) ?? "";
            thinkingBufRef.current.set(pending, buf + delta);
            dirtyRef.current = true;
            scheduleFlush();
          }
          return;
        }

        // Assistant streaming deltas — accumulate, flush on next animation frame
        if (ev.type === "assistant_delta") {
          const delta = ev.summary ?? "";
          if (delta) {
            const buf = deltaBufRef.current.get(pending) ?? "";
            // First token: update message time to now
            if (!buf) {
              const firstTokenTime = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
              updateMessages((prev) =>
                prev.map((m) => m.runId === pending && m.pending ? { ...m, time: firstTokenTime } : m),
              );
            }
            deltaBufRef.current.set(pending, buf + delta);
            dirtyRef.current = true;
            scheduleFlush();
          }
          return;
        }

        // Chat response (partial or final) from "chat" event path
        if (ev.type === "chat_response") {
          const d = ev.detail as Record<string, any> | undefined;
          const chatMsg = d?.message ?? ev.summary ?? "";

          if (chatMsg === "[no reply]") {
            const { text, savedChips, usage } = resolveRun(pending);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: text === "…" ? "…" : text, pending: false, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          if (ev.state === "complete" || ev.state === "final") {
            const finalText = chatMsg || deltaBufRef.current.get(pending) || "";
            // Skip empty finals — OpenClaw sends acknowledgment-only finals with no message.
            // Wait for the actual response or let the timeout handle it.
            if (!finalText) return;
            const { savedChips, usage } = resolveRun(pending);
            const cleaned = stripHWMarkers(finalText);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: cleaned, pending: false, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          if (ev.state === "error") {
            const errMsg = (ev.detail as Record<string, string>)?.error ?? ev.summary ?? "error";
            const { savedChips, usage } = resolveRun(pending);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: errMsg, pending: false, error: true, tools: savedChips, tokenUsage: usage }
                  : m,
              ),
            );
            return;
          }

          // Partial (non-delta path)
          if (chatMsg && !deltaBufRef.current.has(pending)) {
            const cleaned = stripHWMarkers(chatMsg);
            updateMessages((prev) =>
              prev.map((m) =>
                m.runId === pending && m.role === "agent" && m.pending
                  ? { ...m, text: cleaned }
                  : m,
              ),
            );
          }
        }
      } catch {
        // ignore malformed SSE data
      }
    };

    const open = () => {
      if (es !== null) return;
      es = new EventSource(`${API}/agent/events`, { withCredentials: true });
      es.onmessage = onMessage;
    };
    const close = () => {
      if (es !== null) { es.close(); es = null; }
    };
    const shouldBeOpen = () => isActive && !document.hidden;
    const onVisibility = () => {
      if (shouldBeOpen()) open(); else close();
    };

    if (shouldBeOpen()) open();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close();
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
  }, [updateMessages, isActive]);

  // Watch flow events for final response (tts_send, no_reply from JSONL)
  // This catches responses that only appear in flow logs, not on the live bus
  useEffect(() => {
    const pending = pendingRunIdRef.current;
    if (!pending || resolvedIds.current.has(pending)) return;

    // Steered/merged pattern: OpenClaw closes the device run with chat_final_empty
    // and re-fires the same input under a fresh UUID-keyed turn (see
    // docs/debug/openclaw-selfreplay.md). The actual reply (tts_send,
    // lifecycle_end, etc.) flows under the UUID, not the device run id, so
    // the loop below would never see it without this pairing.
    //
    // Pair by matching the user's outgoing text against a chat_input
    // (source=channel) that arrives AFTER chat_final_empty in the events
    // array — `events` is oldest-first, so index comparison gives temporal
    // order. The forward-only scan prevents accidental pairing with an
    // earlier same-text Telegram turn from elsewhere in the day.
    //
    // Exact-equal after prefix strip — Flow Monitor's pair-tint uses
    // substring containment with a 32-char guard, but we have the user's
    // full original text in pendingUserTextRef so exact match works for
    // short inputs like "hello" too.
    const acceptedRunIds = new Set<string>([pending]);
    const userText = pendingUserTextRef.current;
    if (userText) {
      let emptyIdx = -1;
      for (let i = 0; i < events.length; i++) {
        const ev = events[i];
        if (ev.type !== "flow_event") continue;
        const d = ev.detail as any;
        if (d?.node !== "chat_final_empty") continue;
        const r = ev.runId ?? d?.run_id ?? d?.data?.run_id;
        if (r === pending) { emptyIdx = i; break; }
      }
      if (emptyIdx >= 0) {
        const expected = userText.trim().toLowerCase();
        for (let i = emptyIdx + 1; i < events.length; i++) {
          const ev = events[i];
          if (ev.type !== "flow_event") continue;
          const d = ev.detail as any;
          if (d?.node !== "chat_input") continue;
          if (d?.data?.source !== "channel") continue;
          const msg = String(d?.data?.message ?? "");
          if (!msg) continue;
          const norm = msg.replace(/^\[[^\]]+\]\s*/, "").trim().toLowerCase();
          if (norm !== expected) continue;
          const uuidId = d?.data?.run_id ?? ev.runId;
          if (uuidId && uuidId !== pending) {
            acceptedRunIds.add(uuidId);
            break;
          }
        }
      }
    }

    for (const ev of [...events].reverse()) {
      const evRunId: string | undefined =
        ev.runId ??
        (ev.detail as any)?.run_id ??
        (ev.detail as any)?.runId ??
        (ev.detail as any)?.data?.run_id;
      if (!evRunId || !acceptedRunIds.has(evRunId)) continue;

      const d = ev.detail as Record<string, any> | undefined;
      if (ev.type === "flow_event" && (d?.node === "tts_send" || d?.node === "tts_suppressed")) {
        const text: string = d?.data?.text ?? d?.text ?? "";
        if (text) {
          resolvedIds.current.add(pending);
          pendingRunIdRef.current = null;
          setSending(false);
          setThinkingText(null);
          const chips = Array.from(toolChipsRef.current.values());
          const savedChips = chips.length > 0 ? chips : undefined;
          toolChipsRef.current.clear();
          setToolChips([]);
          const usage = tokenUsageRef.current;
          tokenUsageRef.current = undefined;
          const cleaned = stripHWMarkers(text);
          updateMessages((prev) =>
            prev.map((m) =>
              m.runId === pending && m.role === "agent" && m.pending
                ? { ...m, text: cleaned, pending: false, tools: savedChips, tokenUsage: usage }
                : m,
            ),
          );
          return;
        }
      }
      if (ev.type === "flow_event" && d?.node === "no_reply") {
        resolvedIds.current.add(pending);
        pendingRunIdRef.current = null;
        setSending(false);
        setThinkingText(null);
        toolChipsRef.current.clear();
        setToolChips([]);
        tokenUsageRef.current = undefined;
        updateMessages((prev) =>
          prev.map((m) =>
            m.runId === pending && m.role === "agent" && m.pending
              ? { ...m, text: "…", pending: false }
              : m,
          ),
        );
        return;
      }
    }
  }, [events, updateMessages]);

  // Scroll to bottom on conversation switch
  useEffect(() => {
    // Use setTimeout to let DOM render messages first
    setTimeout(() => {
      const el = scrollContainerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, 50);
  }, [activeId]);

  // Scroll to bottom when chat tab becomes active. Chat stays mounted with
  // display:none on other tabs, so scrollHeight is 0 during background render
  // — auto-scroll on activeId/messages never lands the user at the bottom on
  // first reveal. Jumping on isActive flip closes the gap.
  useEffect(() => {
    if (!isActive) return;
    setTimeout(() => {
      const el = scrollContainerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }, 50);
  }, [isActive]);

  // Auto-scroll on new messages — always scroll if last message is pending (streaming)
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const lastMsg = messages[messages.length - 1];
    const hasPending = lastMsg?.pending;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
    if (nearBottom || hasPending) scrollToBottom();
  }, [messages, scrollToBottom]);

  // ─── Actions ────────────────────────────────────────────────────────────

  const newChat = useCallback(() => {
    if (active && active.messages.length === 0) return;
    const id = `c-${Date.now()}`;
    const convo: Conversation = { id, title: "New chat", createdAt: Date.now(), messages: [] };
    setConvos((prev) => [convo, ...prev]);
    setActiveId(id);
    setSending(false);
    pendingRunIdRef.current = null;
    setTimeout(() => textareaRef.current?.focus(), 50);
  }, [active]);

  const switchTo = (id: string) => {
    if (id === activeId) return;
    setActiveId(id);
    setSending(false);
    pendingRunIdRef.current = null;
  };

  const deleteConvo = (id: string) => {
    if (confirmDeleteId !== id) {
      setConfirmDeleteId(id);
      setTimeout(() => setConfirmDeleteId((prev) => prev === id ? null : prev), 3000);
      return;
    }
    setConvos((prev) => prev.filter((c) => c.id !== id));
    if (activeId === id) setActiveId(null);
    setConfirmDeleteId(null);
  };

  const togglePin = (id: string) => {
    setConvos((prev) => prev.map((c) => c.id === id ? { ...c, pinned: !c.pinned } : c));
  };

  const startRename = (c: Conversation) => {
    setEditingId(c.id);
    setEditTitle(c.title);
  };

  const commitRename = () => {
    if (!editingId) return;
    const trimmed = editTitle.trim();
    if (trimmed) {
      setConvos((prev) =>
        prev.map((c) => c.id === editingId ? { ...c, title: trimmed, manualTitle: true } : c),
      );
    }
    setEditingId(null);
  };

  const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

  const attachFile = useCallback((file: File) => {
    if (file.size > MAX_FILE_SIZE) {
      alert(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max 10 MB.`);
      return;
    }
    const isImage = file.type.startsWith("image/");
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      setFileBase64(dataUrl.split(",")[1] ?? null);
      setFileName(file.name);
      setFileSize(file.size);
      setFileIsImage(isImage);
      setFilePreview(isImage ? dataUrl : null);
    };
    reader.readAsDataURL(file);
  }, []);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) attachFile(file);
    e.target.value = "";
  };

  const clearFile = () => {
    setFilePreview(null);
    setFileBase64(null);
    setFileName(null);
    setFileSize(0);
    setFileIsImage(false);
  };

  // Drag & drop
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(true);
  }, []);
  const onDragLeave = useCallback((e: React.DragEvent) => {
    // Only leave when exiting the container (not children)
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragging(false);
  }, []);
  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) attachFile(file);
  }, [attachFile]);

  // Paste image from clipboard
  const onPaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.startsWith("image/")) {
        const file = items[i].getAsFile();
        if (file) {
          e.preventDefault();
          attachFile(file);
          return;
        }
      }
    }
  }, [attachFile]);

  const exportConversation = () => {
    if (!active || active.messages.length === 0) return;
    const lines = active.messages.map((m) => {
      const role = m.role === "user" ? "You" : "Assistant";
      return `[${m.time}] ${role}: ${m.text}`;
    });
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `chat-${active.id}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const copyMessage = (msg: ChatMessage) => {
    copyToClipboard(msg.text).then(() => {
      setCopiedId(msg.id);
      setTimeout(() => setCopiedId((prev) => prev === msg.id ? null : prev), 1500);
    });
  };

  const retryMessage = (errorMsg: ChatMessage) => {
    // Find the user message right before this error
    const idx = messages.findIndex((m) => m.id === errorMsg.id);
    if (idx < 1) return;
    const userMsg = messages[idx - 1];
    if (userMsg.role !== "user") return;

    // Remove the error message and resend
    updateMessages((prev) => prev.filter((m) => m.id !== errorMsg.id));
    setInput(userMsg.text);
    // Remove the user message too, send() will re-add it
    setTimeout(() => {
      updateMessages((prev) => prev.filter((m) => m.id !== userMsg.id));
      // Trigger send with the text
      sendText(userMsg.text);
    }, 50);
  };

  // ─── Send logic ─────────────────────────────────────────────────────────

  const sendText = useCallback(async (text: string, attachedImage?: string | null) => {
    if (!text || sending) return;

    let targetId = activeId;
    if (!targetId) {
      const id = `c-${Date.now()}`;
      const convo: Conversation = { id, title: "New chat", createdAt: Date.now(), messages: [] };
      setConvos((prev) => [convo, ...prev]);
      setActiveId(id);
      targetId = id;
    }

    const nowDate = new Date();
    const now = nowDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const dateStr = nowDate.toISOString().slice(0, 10);
    const userMsg: ChatMessage = {
      id: `u-${Date.now()}`, role: "user", text, time: now, date: dateStr,
      imageUrl: filePreview ?? undefined,
      fileName: (!fileIsImage && fileName) ? fileName : undefined,
      fileSize: (!fileIsImage && fileSize) ? fileSize : undefined,
    };

    setConvos((prev) =>
      prev.map((c) => {
        if (c.id !== targetId) return c;
        const msgs = [...c.messages, userMsg];
        const title = !c.manualTitle ? titleFromMessages(msgs) : c.title;
        return { ...c, messages: msgs, title };
      }),
    );
    setInput("");
    clearFile();
    setSending(true);
    setTimeout(scrollToBottom, 50);

    const sendImage = attachedImage ?? fileBase64;

    try {
      const body: Record<string, string> = { type: "web_chat", message: text };
      if (sendImage) body.image = sendImage;
      const res = await fetch(`${API}/sensing/event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json();

      if (json.status === 1 && json.data?.runId) {
        const runId: string = json.data.runId;
        pendingRunIdRef.current = runId;
        pendingUserTextRef.current = text;
        const replyTime = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: [...c.messages, { id: `l-${runId}`, role: "agent", text: "", time: replyTime, runId, pending: true }] }
              : c,
          ),
        );
        setTimeout(() => {
          if (pendingRunIdRef.current === runId) {
            pendingRunIdRef.current = null;
            setSending(false);
            setThinkingText(null);
            setToolChips([]);
            const streamed = deltaBufRef.current.get(runId);
            deltaBufRef.current.delete(runId);
            thinkingBufRef.current.delete(runId);
            toolChipsRef.current.clear();
            setConvos((prev) =>
              prev.map((c) =>
                c.id === targetId
                  ? { ...c, messages: c.messages.map((m) => m.runId === runId && m.pending
                      ? { ...m, text: streamed || "⏱ no response", pending: false, error: !streamed }
                      : m) }
                  : c,
              ),
            );
          }
        }, 120_000);
      } else if (json.data?.handler === "local") {
        setSending(false);
        const localText = json.data?.response || "✓ handled locally";
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: [...c.messages, { id: `l-local-${Date.now()}`, role: "agent", text: localText, time: now }] }
              : c,
          ),
        );
      } else if (json.data?.handler === "dropped" || json.data?.handler === "queued") {
        setSending(false);
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: [...c.messages, { id: `l-drop-${Date.now()}`, role: "agent", text: "⏸ busy — try again", time: now, error: true }] }
              : c,
          ),
        );
      } else {
        setSending(false);
        setConvos((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, messages: [...c.messages, { id: `l-err-${Date.now()}`, role: "agent", text: json.message ?? "error", time: now, error: true }] }
              : c,
          ),
        );
      }
    } catch {
      setSending(false);
      setConvos((prev) =>
        prev.map((c) =>
          c.id === targetId
            ? { ...c, messages: [...c.messages, { id: `l-err-${Date.now()}`, role: "agent", text: "connection error", time: now, error: true }] }
            : c,
        ),
      );
    }
  }, [activeId, sending, updateMessages, filePreview, fileBase64, fileIsImage, fileName, fileSize]);

  const send = () => { sendText(input.trim(), fileBase64); };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  };

  // ─── Filtered conversations ─────────────────────────────────────────────
  const filtered = search.trim()
    ? convos.filter((c) => {
        const q = search.toLowerCase();
        if (c.title.toLowerCase().includes(q)) return true;
        return c.messages.some((m) => m.text.toLowerCase().includes(q));
      })
    : convos;
  const grouped = groupConvosByDate(filtered);

  // ─── Render ─────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", height: "100%", gap: 0, position: "relative" }}>
      {/* History panel is persistent — clicking outside does NOT close it.
          Only the explicit collapse button (▶ in the panel header) closes it.
          Lives as a normal flex item (NOT absolute) so opening it pushes the
          chat area narrower instead of overlaying it. */}
      {sidebarOpen && (
      <div style={{
        width: 280, flexShrink: 0, order: 2,
        borderLeft: "1px solid var(--lm-border)",
        display: "flex", flexDirection: "column",
        background: "var(--lm-sidebar)",
      }}>
        {/* Header: title strip + actions. Consistent padding with rest of sidebar (14px horizontal). */}
        <div style={{
          padding: "14px 14px 10px",
          borderBottom: "1px solid var(--lm-border)",
          display: "flex", flexDirection: "column", gap: 10,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <History size={14} style={{ color: "var(--lm-text-muted)" }} />
            <span style={{
              fontSize: 10, fontWeight: 700, color: "var(--lm-text-muted)",
              textTransform: "uppercase", letterSpacing: "0.08em",
            }}>History</span>
            <span style={{ flex: 1 }} />
            <button
              onClick={() => setSidebarOpen(false)}
              style={{
                width: 24, height: 24, padding: 0, borderRadius: 5,
                background: "transparent", border: "none",
                color: "var(--lm-text-muted)",
                cursor: "pointer", flexShrink: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
              title="Hide history"
              aria-label="Hide history"
            ><ChevronRight size={14} /></button>
          </div>
          <button
            onClick={newChat}
            title="New chat (Ctrl+N)"
            style={{
              width: "100%", padding: "9px 12px", borderRadius: 8,
              background: "color-mix(in srgb, var(--lm-amber) 14%, transparent)",
              border: "1px solid color-mix(in srgb, var(--lm-amber) 30%, transparent)",
              color: "var(--lm-amber)", fontSize: 12, fontWeight: 600,
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 6,
            }}
          >
            <Plus size={14} /> New chat
          </button>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search chats…"
            style={{
              width: "100%", padding: "7px 10px", borderRadius: 6,
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: "var(--lm-text)", fontSize: 11.5, outline: "none",
              boxSizing: "border-box",
            }}
          />
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "6px 10px 12px" }}>
          {filtered.length === 0 && (
            <div style={{ padding: 16, textAlign: "center", color: "var(--lm-text-muted)", fontSize: 11 }}>
              {search ? "No matches" : "No conversations yet"}
            </div>
          )}
          {grouped.map(({ label, items }) => (
            <div key={label} style={{ marginBottom: 6 }}>
              <div style={{
                fontSize: 9.5, fontWeight: 700, color: "var(--lm-text-muted)",
                padding: "10px 6px 6px", textTransform: "uppercase", letterSpacing: "0.06em",
              }}>
                {label}
              </div>
              {items.map((c) => {
                const isActive = c.id === activeId;
                const isHovered = hoveredConvoId === c.id;
                return (
                <div
                  key={c.id}
                  onClick={() => switchTo(c.id)}
                  onMouseEnter={() => setHoveredConvoId(c.id)}
                  onMouseLeave={() => setHoveredConvoId((cur) => (cur === c.id ? null : cur))}
                  style={{
                    position: "relative",
                    padding: "8px 10px", borderRadius: 7, cursor: "pointer",
                    background: isActive
                      ? "color-mix(in srgb, var(--lm-amber) 14%, transparent)"
                      : isHovered ? "color-mix(in srgb, var(--lm-text) 4%, transparent)" : "transparent",
                    marginBottom: 2,
                    transition: "background 0.15s",
                  }}
                >
                  {/* Pin badge — small inline marker top-right when pinned */}
                  {c.pinned && !isHovered && (
                    <span style={{
                      position: "absolute", top: 6, right: 8,
                      color: "var(--lm-amber)", display: "flex", alignItems: "center",
                      pointerEvents: "none",
                    }}><Pin size={10} fill="currentColor" /></span>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {editingId === c.id ? (
                        <input
                          autoFocus
                          value={editTitle}
                          onChange={(e) => setEditTitle(e.target.value)}
                          onBlur={commitRename}
                          onKeyDown={(e) => { if (e.key === "Enter") commitRename(); if (e.key === "Escape") setEditingId(null); }}
                          onClick={(e) => e.stopPropagation()}
                          style={{
                            fontSize: 12, width: "100%", background: "var(--lm-surface)",
                            border: "1px solid var(--lm-amber)", borderRadius: 4,
                            color: "var(--lm-text)", padding: "1px 4px", outline: "none",
                          }}
                        />
                      ) : (
                        <div
                          onDoubleClick={(e) => { e.stopPropagation(); startRename(c); }}
                          title="Double-click to rename"
                          style={{
                            fontSize: 12.5,
                            color: isActive ? "var(--lm-amber)" : "var(--lm-text)",
                            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                            fontWeight: isActive ? 600 : 500,
                            paddingRight: c.pinned ? 14 : 0,
                          }}
                        >
                          {c.title}
                        </div>
                      )}
                      {c.messages.length > 0 && (
                        <div style={{
                          fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 3,
                          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                        }}>
                          {(() => {
                            const last = c.messages[c.messages.length - 1];
                            const txt = last.text || "…";
                            return (last.role === "agent" ? "↪ " : "") + (txt.length > 40 ? txt.slice(0, 40) + "…" : txt);
                          })()}
                        </div>
                      )}
                    </div>
                    {/* Hover-reveal actions — keep the row clean when idle. */}
                    <div style={{
                      display: "flex", gap: 2, flexShrink: 0,
                      opacity: isHovered ? 1 : 0,
                      pointerEvents: isHovered ? "auto" : "none",
                      transition: "opacity 0.15s",
                    }}>
                      <button
                        onClick={(e) => { e.stopPropagation(); togglePin(c.id); }}
                        style={hoverIconBtnStyle(c.pinned ? "var(--lm-amber)" : "var(--lm-text-muted)")}
                        title={c.pinned ? "Unpin" : "Pin to top"}
                        aria-label={c.pinned ? "Unpin" : "Pin"}
                      >{c.pinned ? <Pin size={12} fill="currentColor" /> : <Pin size={12} />}</button>
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteConvo(c.id); }}
                        style={{
                          ...hoverIconBtnStyle(confirmDeleteId === c.id ? "var(--lm-red)" : "var(--lm-text-muted)"),
                          background: confirmDeleteId === c.id ? "color-mix(in srgb, var(--lm-red) 20%, transparent)" : "transparent",
                        }}
                        title={confirmDeleteId === c.id ? "Click again to confirm" : "Delete"}
                        aria-label="Delete conversation"
                      >{confirmDeleteId === c.id ? <Check size={12} /> : <Trash2 size={12} />}</button>
                    </div>
                  </div>
                </div>
                );
              })}
            </div>
          ))}
        </div>
        {convos.length > 1 && (
          <div style={{ padding: "10px 14px", borderTop: "1px solid var(--lm-border)" }}>
            <button
              onClick={() => {
                if (confirm(`Delete all ${convos.filter((c) => !c.pinned).length} unpinned conversations?`)) {
                  setConvos((prev) => prev.filter((c) => c.pinned));
                  setActiveId(null);
                }
              }}
              style={{
                width: "100%", padding: "6px 0", borderRadius: 6,
                background: "none", border: "1px solid var(--lm-border)",
                color: "var(--lm-text-muted)", fontSize: 10.5, cursor: "pointer",
                transition: "all 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "var(--lm-red)"; e.currentTarget.style.borderColor = "var(--lm-red)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "var(--lm-text-muted)"; e.currentTarget.style.borderColor = "var(--lm-border)"; }}
            >Clear all unpinned</button>
          </div>
        )}
      </div>
      )}

      {/* ── Chat area ── */}
      <div
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, position: "relative" }}
      >
        {/* Drop overlay */}
        {dragging && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 10,
            background: "rgba(245,158,11,0.08)",
            border: "2px dashed var(--lm-amber)",
            borderRadius: 8,
            display: "flex", alignItems: "center", justifyContent: "center",
            pointerEvents: "none",
          }}>
            <span style={{ fontSize: 14, color: "var(--lm-amber)", fontWeight: 600 }}>
              Drop file here
            </span>
          </div>
        )}
        {/* Chat header bar — matches the monitor topbar rhythm (10px/14px pad,
            44px tall) so Chat reads as part of the same shell as Overview /
            System rather than a denser one-off. */}
        <div style={{
          padding: "10px 16px", borderBottom: "1px solid var(--lm-border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
          background: "var(--lm-sidebar)", minHeight: 44, gap: 12,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0, flex: 1 }}>
            <Sparkles size={15} style={{ color: "var(--lm-amber)", flexShrink: 0 }} />
            <span style={{
              fontSize: 14,
              color: active ? "var(--lm-text)" : "var(--lm-text-muted)",
              fontWeight: 700,
              letterSpacing: "-0.01em",
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
            }}>
              {active ? active.title : "Select or start a chat"}
            </span>
          </div>
          {/* Header actions — Export + History sharing the same pill style for a
              uniform right-side toolbar. */}
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            {active && active.messages.length > 0 && (
              <button
                onClick={exportConversation}
                style={headerPillBtnStyle}
                title="Export as text"
                aria-label="Export conversation"
              ><Download size={12} /> Export</button>
            )}
            {convos.length > 0 && (
              <button
                onClick={() => {
                  if (!window.confirm("Clear all local chat history? This wipes the browser cache only — server-side flow logs are untouched.")) return;
                  clearLocalChatHistory();
                  setConvos([]);
                  setActiveId(null);
                }}
                style={headerPillBtnStyle}
                title={`Clear local chat history (auto-purges after ${Math.round(HISTORY_TTL_MS / (24 * 60 * 60 * 1000))}d)`}
                aria-label="Clear local chat history"
              ><Trash2 size={12} /> Clear</button>
            )}
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                style={headerPillBtnStyle}
                title="Show history"
                aria-label="Show history"
              ><History size={13} /> History</button>
            )}
          </div>
        </div>
        {/* Messages — scroll happens on the outer wrapper; the inner column is
            constrained to a comfortable reading width (centered) so messages
            don't sprawl edge-to-edge on wide displays. */}
        <div
          ref={scrollContainerRef}
          onScroll={onScroll}
          style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 16px 8px" }}
        >
          <div style={{ maxWidth: 760, margin: "0 auto", width: "100%", display: "flex", flexDirection: "column", gap: 12 }}>
          {messages.length === 0 && (
            <div style={{ margin: "auto", textAlign: "center", color: "var(--lm-text-muted)", fontSize: 13, lineHeight: 1.8 }}>
              <div style={{ marginBottom: 10, display: "flex", justifyContent: "center", color: "var(--lm-amber)" }}>
                <Sparkles size={28} />
              </div>
              <div>Chat with Assistant</div>
              <div style={{ fontSize: 11, marginTop: 4 }}>Type a message or press Shift+Enter for multi-line</div>
            </div>
          )}
          {messages.map((msg, i) => {
            // Date separator
            const prevDate = i > 0 ? messages[i - 1].date : null;
            const showDate = msg.date && msg.date !== prevDate;
            return (
            <div key={msg.id}>
              {showDate && (
                <div style={{
                  textAlign: "center", fontSize: 10, color: "var(--lm-text-muted)",
                  padding: "8px 0 4px", fontWeight: 500,
                }}>
                  {formatDateLabel(msg.date!)}
                </div>
              )}
              <div
                className="lm-chat-msg"
                style={{ display: "flex", flexDirection: msg.role === "user" ? "row-reverse" : "row", alignItems: "flex-end" }}
              >
              <div style={{ maxWidth: msg.role === "user" ? "72%" : "85%", display: "flex", flexDirection: "column", alignItems: msg.role === "user" ? "flex-end" : "flex-start", gap: 3 }}>
                {/* Sender label for first device message or after user message */}
                {msg.role === "agent" && (i === 0 || messages[i - 1]?.role === "user") && (
                  <span style={{ fontSize: 10, color: "var(--lm-amber)", fontWeight: 600, paddingLeft: 4 }}>Assistant</span>
                )}
                {/* Thinking indicator — shown only for the active pending message */}
                {msg.pending && msg.role === "agent" && msg.runId === pendingRunIdRef.current && thinkingText && (
                  <ThinkingBlock text={thinkingText} />
                )}
                {/* Tool call chips — live during pending, persisted after finalize.
                    Click a chip to expand its args/result panel. */}
                {msg.role === "agent" && (() => {
                  const isActivePending = msg.pending && msg.runId === pendingRunIdRef.current;
                  const chips = isActivePending ? toolChips : msg.tools;
                  if (!chips || chips.length === 0) return null;
                  return (
                    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
                      {chips.map((c) => <ToolChipView key={c.id} chip={c} />)}
                    </div>
                  );
                })()}
                <div style={{
                  padding: "9px 13px",
                  borderRadius: msg.role === "user" ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
                  background: msg.role === "user" ? "rgba(245,158,11,0.15)" : "var(--lm-surface)",
                  border: `1px solid ${msg.role === "user" ? "rgba(245,158,11,0.25)" : "var(--lm-border)"}`,
                  color: msg.error ? "var(--lm-red)" : "var(--lm-text)",
                  fontSize: 13.5, lineHeight: 1.55, wordBreak: "break-word",
                  minWidth: 40, minHeight: 36, position: "relative",
                }}>
                  {msg.imageUrl && (
                    <img
                      src={msg.imageUrl}
                      alt="attached"
                      style={{
                        maxWidth: 200, maxHeight: 150, borderRadius: 6,
                        marginBottom: msg.text ? 6 : 0,
                      }}
                    />
                  )}
                  {msg.fileName && (
                    <div style={{
                      display: "flex", alignItems: "center", gap: 6,
                      padding: "4px 8px", borderRadius: 6,
                      background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                      marginBottom: msg.text ? 6 : 0, fontSize: 11.5,
                    }}>
                      <span>📎</span>
                      <span style={{ color: "var(--lm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 160 }}>
                        {msg.fileName}
                      </span>
                      {msg.fileSize != null && (
                        <span style={{ color: "var(--lm-text-muted)", fontSize: 10, flexShrink: 0 }}>
                          {msg.fileSize < 1024 ? `${msg.fileSize} B`
                            : msg.fileSize < 1024 * 1024 ? `${(msg.fileSize / 1024).toFixed(0)} KB`
                            : `${(msg.fileSize / 1024 / 1024).toFixed(1)} MB`}
                        </span>
                      )}
                    </div>
                  )}
                  {msg.pending && !msg.text ? (
                    <span style={{ color: "var(--lm-text-muted)" }}>
                      <span className="lm-blink">●</span>
                      <span style={{ marginLeft: 4 }}>●</span>
                      <span style={{ marginLeft: 4 }}>●</span>
                    </span>
                  ) : msg.pending && msg.text ? (
                    <>
                      {msg.role === "agent" ? renderMarkdown(msg.text) : msg.text}
                      <span className="lm-cursor" style={{
                        display: "inline-block", width: 2, height: "1em",
                        background: "var(--lm-amber)", marginLeft: 2,
                        verticalAlign: "text-bottom", borderRadius: 1,
                      }} />
                    </>
                  ) : msg.role === "agent" ? renderMarkdown(msg.text) : msg.text}
                </div>
                {/* Action bar: time + copy + retry */}
                <div style={{ display: "flex", alignItems: "center", gap: 6, paddingInline: 4 }}>
                  <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>{msg.time}</span>
                  {!msg.pending && msg.text && msg.text !== "…" && (
                    <button
                      onClick={() => copyMessage(msg)}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        color: copiedId === msg.id ? "var(--lm-green)" : "var(--lm-text-muted)",
                        padding: 0, opacity: 0.6, transition: "opacity 0.15s",
                        display: "inline-flex", alignItems: "center",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.6"; }}
                      title="Copy"
                      aria-label="Copy message"
                    >{copiedId === msg.id ? <Check size={12} /> : <Copy size={12} />}</button>
                  )}
                  {msg.error && msg.role === "agent" && (
                    <button
                      onClick={() => retryMessage(msg)}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        fontSize: 10, color: "var(--lm-amber)", padding: 0,
                        opacity: 0.7, transition: "opacity 0.15s",
                        display: "inline-flex", alignItems: "center", gap: 3,
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.7"; }}
                      title="Retry"
                    ><RotateCcw size={11} /> retry</button>
                  )}
                  {msg.tokenUsage && msg.role === "agent" && <UsageBadge usage={msg.tokenUsage} model={modelLabel} />}
                </div>
              </div>
            </div>
            </div>
            );
          })}
          <div ref={bottomRef} />
          </div>
        </div>

        {/* Scroll to bottom */}
        {showScrollBtn && (
          <button
            onClick={scrollToBottom}
            style={{
              position: "absolute", bottom: 80, right: 20,
              width: 32, height: 32, borderRadius: "50%",
              background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
              color: "var(--lm-text-muted)",
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              boxShadow: "0 2px 8px rgba(0,0,0,0.3)", transition: "opacity 0.2s",
            }}
            title="Scroll to bottom"
            aria-label="Scroll to bottom"
          ><ArrowDown size={16} /></button>
        )}

        {/* Input — ChatGPT-style pill. File preview lives INSIDE the pill (top
            slot) so the attach state is visually part of the input, not a
            separate banner. Centered with max-width like the messages column.
            flexShrink:0 pins the composer so it can never be squeezed/clipped by
            the scroll area above it. */}
        <div style={{
          flexShrink: 0,
          padding: "10px 16px 12px",
          borderTop: "1px solid var(--lm-border)",
          background: "var(--lm-sidebar)",
        }}>
          <div style={{ maxWidth: 760, margin: "0 auto", width: "100%" }}>
            <input ref={fileInputRef} type="file" style={{ display: "none" }} onChange={handleFileSelect} />
            <div
              className="lm-chat-composer"
              style={{
                display: "flex", flexDirection: "column", gap: 6,
                background: "var(--lm-card)",
                border: "1px solid var(--lm-border)",
                borderRadius: 16,
                padding: 6,
                boxShadow: "0 1px 2px rgba(0,0,0,0.18), 0 8px 24px -16px rgba(0,0,0,0.5)",
                transition: "border-color 0.15s, box-shadow 0.15s",
              }}>
              {/* Attached file chip inside the pill — fixed slot above the input row. */}
              {fileName && (
                <div style={{
                  display: "flex", alignItems: "center", gap: 8,
                  padding: "6px 8px",
                  margin: "0 2px",
                  borderRadius: 12,
                  background: "color-mix(in srgb, var(--lm-amber) 8%, transparent)",
                  border: "1px solid color-mix(in srgb, var(--lm-amber) 25%, transparent)",
                }}>
                  {filePreview ? (
                    <img src={filePreview} alt="preview" style={{ height: 36, borderRadius: 6, flexShrink: 0 }} />
                  ) : (
                    <div style={{
                      width: 36, height: 36, borderRadius: 6,
                      background: "color-mix(in srgb, var(--lm-amber) 15%, transparent)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      color: "var(--lm-amber)", flexShrink: 0,
                    }}><Paperclip size={16} /></div>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 11.5, color: "var(--lm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{fileName}</div>
                    <div style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                      {fileSize < 1024 ? `${fileSize} B` : fileSize < 1024 * 1024 ? `${(fileSize / 1024).toFixed(0)} KB` : `${(fileSize / 1024 / 1024).toFixed(1)} MB`}
                    </div>
                  </div>
                  <button
                    onClick={clearFile}
                    style={{
                      background: "transparent", border: "none", cursor: "pointer",
                      color: "var(--lm-text-muted)", padding: 4, borderRadius: 4,
                      display: "flex", alignItems: "center",
                    }}
                    title="Remove file"
                    aria-label="Remove file"
                  ><X size={14} /></button>
                </div>
              )}

              <div style={{
                display: "flex", alignItems: "flex-end", gap: 6,
              }}>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={sending}
                style={{
                  background: "transparent", border: "none",
                  borderRadius: "50%", width: 34, height: 34,
                  cursor: sending ? "default" : "pointer",
                  color: "var(--lm-text-muted)", flexShrink: 0,
                  opacity: sending ? 0.5 : 0.85, transition: "opacity 0.15s, background 0.15s",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}
                onMouseEnter={(e) => { if (!sending) { e.currentTarget.style.opacity = "1"; e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 8%, transparent)"; } }}
                onMouseLeave={(e) => { e.currentTarget.style.opacity = sending ? "0.5" : "0.85"; e.currentTarget.style.background = "transparent"; }}
                title="Attach file (max 10 MB)"
                aria-label="Attach file"
              ><Paperclip size={17} /></button>
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                onPaste={onPaste}
                disabled={sending}
                placeholder="Message Assistant…"
                rows={1}
                style={{
                  flex: 1, minWidth: 0,
                  background: "transparent", border: "none",
                  padding: "8px 4px",
                  color: "var(--lm-text)", fontSize: 14,
                  outline: "none", opacity: sending ? 0.6 : 1,
                  resize: "none", lineHeight: 1.5, fontFamily: "inherit",
                  minHeight: 22, maxHeight: 200, overflow: "auto",
                  boxSizing: "border-box",
                }}
              />
              <button
                onClick={send}
                disabled={!input.trim() || sending}
                style={{
                  width: 34, height: 34, borderRadius: "50%", flexShrink: 0,
                  background: input.trim() && !sending
                    ? "var(--lm-amber)"
                    : "color-mix(in srgb, var(--lm-text) 15%, transparent)",
                  border: "none",
                  color: input.trim() && !sending ? "var(--lm-on-amber)" : "var(--lm-text-muted)",
                  cursor: input.trim() && !sending ? "pointer" : "default",
                  transition: "all 0.15s",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                }}
                title="Send (Enter)"
                aria-label="Send message"
              >{sending ? <span style={{ fontSize: 14, fontWeight: 700 }}>…</span> : <ArrowDown size={16} style={{ transform: "rotate(180deg)" }} strokeWidth={2.5} />}</button>
              </div>
            </div>
            <div style={{
              fontSize: 10, color: "var(--lm-text-muted)",
              textAlign: "center", marginTop: 6, opacity: 0.7,
            }}>
              Press Enter to send · Shift+Enter for new line
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Usage Badge ─────────────────────────────────────────────────────────────

// Claude 4.x context window — used to derive a "% ctx" indicator. If you
// switch models you can bump this constant or fetch it from openclaw config.
const CONTEXT_WINDOW = 200_000;

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  const k = n / 1000;
  return k >= 100 ? `${k.toFixed(0)}k` : `${k.toFixed(1)}k`;
}

// Compact one-line usage strip under each device message — mirrors the style
// of agent CLIs: ↑input  ↓output  R<cacheRead>  N% ctx  model.
function UsageBadge({ usage, model }: { usage: NonNullable<ChatMessage["tokenUsage"]>; model?: string }) {
  const ctxPct = usage.total > 0 ? Math.min(100, (usage.total / CONTEXT_WINDOW) * 100) : 0;
  return (
    <span
      style={{
        fontSize: 9.5, color: "var(--lm-text-muted)",
        fontFamily: "monospace", opacity: 0.75,
        display: "inline-flex", gap: 10, alignItems: "center",
        whiteSpace: "nowrap",
      }}
      title={
        `Input: ${usage.input.toLocaleString()}\n` +
        `Output: ${usage.output.toLocaleString()}\n` +
        (usage.cacheRead ? `Cache read: ${usage.cacheRead.toLocaleString()}\n` : "") +
        (usage.cacheWrite ? `Cache write: ${usage.cacheWrite.toLocaleString()}\n` : "") +
        `Total: ${usage.total.toLocaleString()}\n` +
        `Context: ${ctxPct.toFixed(1)}% of ${CONTEXT_WINDOW.toLocaleString()}`
      }
    >
      <span>↑{formatTokens(usage.input)}</span>
      <span>↓{formatTokens(usage.output)}</span>
      {usage.cacheRead != null && usage.cacheRead > 0 && (
        <span>R{formatTokens(usage.cacheRead)}</span>
      )}
      <span style={{ color: ctxPct > 80 ? "var(--lm-red)" : ctxPct > 60 ? "var(--lm-amber)" : "var(--lm-text-muted)" }}>
        {ctxPct.toFixed(0)}% ctx
      </span>
      {model && (
        <span style={{ color: "var(--lm-text-dim)" }}>{model}</span>
      )}
    </span>
  );
}

// ─── Tool Chip ───────────────────────────────────────────────────────────────

// Collapsed: pill with icon, name, and a one-line headline (truncated query).
// Expanded: also shows the full args block (JSON) plus a completion marker.
function ToolChipView({ chip }: { chip: ToolChip }) {
  const [open, setOpen] = useState(false);
  const hasDetail = chip.args || chip.detail || chip.result;
  const accent = "var(--lm-teal)";
  return (
    <div style={{
      display: "inline-flex", flexDirection: "column",
      maxWidth: "100%", minWidth: 0,
      borderRadius: 10,
      background: `color-mix(in srgb, ${accent} 10%, transparent)`,
      border: `1px solid color-mix(in srgb, ${accent} 22%, transparent)`,
      color: accent,
    }}>
      <button
        onClick={() => hasDetail && setOpen((v) => !v)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          padding: "3px 9px",
          background: "transparent", border: "none",
          color: "inherit",
          cursor: hasDetail ? "pointer" : "default",
          fontSize: 10.5,
          textAlign: "left",
          minWidth: 0,
        }}
        title={hasDetail ? (open ? "Hide details" : "Show details") : undefined}
      >
        {renderToolIcon(chip.iconKind, 12)}
        <strong style={{ fontWeight: 700 }}>{chip.label}</strong>
        {chip.detail && (
          <span style={{
            opacity: 0.85, fontFamily: "monospace", fontSize: 10,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            maxWidth: 320,
          }}>{chip.detail}</span>
        )}
        {chip.result && (
          <span style={{
            fontSize: 9, padding: "0 5px", borderRadius: 3,
            background: "color-mix(in srgb, var(--lm-green) 18%, transparent)",
            color: "var(--lm-green)", fontWeight: 700, letterSpacing: "0.04em",
          }}>OK</span>
        )}
        {hasDetail && (
          <span style={{ marginLeft: 2, opacity: 0.7, display: "inline-flex" }}>
            {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </span>
        )}
      </button>
      {open && (
        <div style={{
          padding: "6px 10px 8px",
          borderTop: `1px solid color-mix(in srgb, ${accent} 18%, transparent)`,
          background: "color-mix(in srgb, var(--lm-text) 4%, transparent)",
          display: "flex", flexDirection: "column", gap: 6,
          maxWidth: 560,
        }}>
          {chip.args && (
            <div>
              <div style={{
                fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
                color: "var(--lm-text-muted)", marginBottom: 3,
              }}>ARGS</div>
              <pre style={{
                margin: 0, padding: 0,
                fontSize: 10, lineHeight: 1.45, fontFamily: "monospace",
                color: "var(--lm-text-dim)",
                whiteSpace: "pre-wrap", overflowWrap: "anywhere",
                maxHeight: 200, overflowY: "auto",
              }}>{JSON.stringify(chip.args, null, 2)}</pre>
            </div>
          )}
          {chip.result && chip.result !== "completed" && (
            <div>
              <div style={{
                fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
                color: "var(--lm-text-muted)", marginBottom: 3,
              }}>RESULT</div>
              <pre style={{
                margin: 0, padding: 0,
                fontSize: 10, lineHeight: 1.45, fontFamily: "monospace",
                color: "var(--lm-text)",
                whiteSpace: "pre-wrap", overflowWrap: "anywhere",
                maxHeight: 200, overflowY: "auto",
              }}>{chip.result}</pre>
              <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginTop: 3, fontStyle: "italic" }}>
                (truncated to 100 chars by server)
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Thinking Block ──────────────────────────────────────────────────────────

function ThinkingBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const preview = text.length > 80 ? text.slice(0, 80) + "…" : text;

  return (
    <div style={{
      fontSize: 11, lineHeight: 1.5, borderRadius: 8,
      border: "1px solid rgba(168,85,247,0.2)",
      background: "rgba(168,85,247,0.06)",
      overflow: "hidden",
    }}>
      <button
        onClick={() => setExpanded((p) => !p)}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          width: "100%", padding: "6px 10px",
          background: "none", border: "none", cursor: "pointer",
          color: "rgba(168,85,247,0.8)", fontSize: 11, fontWeight: 600,
          textAlign: "left",
        }}
      >
        <span className="lm-blink" style={{ fontSize: 8 }}>●</span>
        <span>Thinking</span>
        <span style={{ fontSize: 9, opacity: 0.6 }}>{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded ? (
        <div style={{
          padding: "0 10px 8px", color: "var(--lm-text-muted)",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: 200, overflowY: "auto",
        }}>
          {text}
        </div>
      ) : (
        <div style={{
          padding: "0 10px 6px", color: "var(--lm-text-muted)",
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }}>
          {preview}
        </div>
      )}
    </div>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDateLabel(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = today.getTime() - d.getTime();
  if (diff <= 0) return "Today";
  if (diff <= 86400_000) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function groupConvosByDate(convos: Conversation[]): { label: string; items: Conversation[] }[] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = today - 86400_000;
  const weekAgo = today - 7 * 86400_000;

  // Pinned first
  const pinned = convos.filter((c) => c.pinned);
  const unpinned = convos.filter((c) => !c.pinned);

  const groups: Record<string, Conversation[]> = {};
  const order: string[] = [];

  if (pinned.length > 0) {
    groups["Pinned"] = pinned;
    order.push("Pinned");
  }

  for (const c of unpinned) {
    let label: string;
    if (c.createdAt >= today) label = "Today";
    else if (c.createdAt >= yesterday) label = "Yesterday";
    else if (c.createdAt >= weekAgo) label = "This week";
    else label = "Older";

    if (!groups[label]) { groups[label] = []; order.push(label); }
    groups[label].push(c);
  }

  return order.map((label) => ({ label, items: groups[label] }));
}
