import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getApiToken, withApiToken } from "@/lib/api";
import { S } from "./styles";
import { API } from "./types";

type LogSource = "hal" | "lamp" | "openclaw" | "openclaw-service" | "buddy";
const LOG_SOURCES: { id: LogSource; label: string; color: string }[] = [
  { id: "hal",              label: "HAL",        color: "var(--lm-green)" },
  { id: "lamp",             label: "Lamp",       color: "var(--lm-amber)" },
  { id: "openclaw",         label: "Agent",      color: "var(--lm-blue)" },
  { id: "openclaw-service", label: "Agent Service", color: "var(--lm-purple)" },
  { id: "buddy",            label: "Buddy",      color: "var(--lm-cyan)" },
];

const LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARN", "ERROR"] as const;
type LogLevel = (typeof LOG_LEVELS)[number];

// Word-boundary level detection — avoids false positives like `error_count=0`
// reporting as ERROR. Looks for the level token surrounded by non-word chars
// or at start/end of line.
const LEVEL_RE = {
  ERROR: /\b(ERROR|ERR)\b/i,
  WARN:  /\b(WARN(?:ING)?)\b/i,
  DEBUG: /\b(DEBUG|DBG)\b/i,
  INFO:  /\b(INFO|INF)\b/i,
};
function detectLevel(line: string): LogLevel {
  if (LEVEL_RE.ERROR.test(line)) return "ERROR";
  if (LEVEL_RE.WARN.test(line))  return "WARN";
  if (LEVEL_RE.DEBUG.test(line)) return "DEBUG";
  if (LEVEL_RE.INFO.test(line))  return "INFO";
  return "ALL";
}

const levelColor: Record<LogLevel, string> = {
  ALL: "var(--lm-text-dim)",
  DEBUG: "var(--lm-purple)",
  INFO: "var(--lm-text-dim)",
  WARN: "var(--lm-amber)",
  ERROR: "var(--lm-red)",
};

// Strip ANSI escape codes only when prefixed by ESC. The earlier fallback that
// matched any `[…m` ate parts of content like `[200ms]` from app logs.
const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, "");

function LogPanel({ source, label, color, initialFilter, initialLevel, onFilterChange }: {
  source: LogSource; label: string; color: string;
  initialFilter: string; initialLevel: LogLevel;
  onFilterChange: (source: LogSource, filter: string, level: LogLevel) => void;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [lastN, setLastN] = useState(200);
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState(initialFilter);
  const [level, setLevel] = useState<LogLevel>(initialLevel);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sseRef = useRef<EventSource | null>(null);


  const fetchLines = useCallback(async () => {
    setLoading(true);
    try {
      const token = getApiToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const resp = await fetch(`${API}/logs/tail?source=${source}&lines=${lastN}`, { headers });
      if (!resp.ok) {
        setError(`HTTP ${resp.status} ${resp.statusText}`);
        setLines([]);
        return;
      }
      const r = await resp.json();
      const data = r?.data;
      if (data?.error) setError(data.error);
      else setError(null);
      setLines(Array.isArray(data?.lines) ? data.lines.map(stripAnsi) : []);
    } catch (e) {
      setError(`Fetch error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  }, [source, lastN]);

  useEffect(() => { fetchLines(); }, [fetchLines]);

  useEffect(() => {
    if (paused) return;

    // Gate the log-stream EventSource on tab visibility. Without this the
    // stream stays connected (and a TCP slot occupied) even when the user
    // is on another browser tab, contributing to the monitor page's
    // connection-pool starvation.
    let es: EventSource | null = null;
    const onLog = (e: Event) => {
      const line = stripAnsi((e as MessageEvent).data);
      if (line) setLines((prev) => [...prev.slice(-4999), line]);
    };
    const open = () => {
      if (es !== null) return;
      // EventSource can't set custom headers; cookies attach automatically
      // with `withCredentials: true` for same-origin connections. Legacy
      // Bearer fallback (?token=) still rides along when a token is set.
      es = new EventSource(withApiToken(`${API}/logs/stream?source=${source}`), { withCredentials: true });
      sseRef.current = es;
      es.addEventListener("log", onLog);
      es.addEventListener("error", () => { /* EventSource auto-reconnects */ });
    };
    const close = () => {
      if (es !== null) { es.close(); es = null; sseRef.current = null; }
    };
    const onVisibility = () => {
      if (document.hidden) close(); else open();
    };
    if (!document.hidden) open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      close();
    };
  }, [source, paused, fetchLines]);

  const filtered = useMemo(() => {
    let result = lines;
    if (level !== "ALL") {
      const levelIdx = LOG_LEVELS.indexOf(level);
      result = result.filter((l) => {
        const ll = detectLevel(l);
        return ll === "ALL" || LOG_LEVELS.indexOf(ll) >= levelIdx;
      });
    }
    if (filter.trim()) {
      try {
        const re = new RegExp(filter, "i");
        result = result.filter((l) => re.test(l));
      } catch {
        const lower = filter.toLowerCase();
        result = result.filter((l) => l.toLowerCase().includes(lower));
      }
    }
    return result;
  }, [lines, level, filter]);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filtered, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
  };

  const highlightLine = (line: string) => {
    if (!filter.trim()) return formatLine(line);
    try {
      // Capture group + split → every other piece is a match. This avoids
      // re.test() with the /g flag mutating lastIndex between checks.
      const re = new RegExp(`(${filter})`, "gi");
      const parts = line.split(re);
      if (parts.length <= 1) return formatLine(line);
      return parts.map((p, i) =>
        i % 2 === 1
          ? <mark key={i} style={{ background: "rgba(245,158,11,0.4)", color: "inherit", borderRadius: 2, padding: "0 1px" }}>{p}</mark>
          : p,
      );
    } catch {
      return formatLine(line);
    }
  };

  // Format log line: dim timestamp, bold level, dim %key=value metadata
  const formatLine = (line: string) => {
    // LeLamp: 2026-04-13 17:47:52,944 INFO lelamp.voice: message
    const pyMatch = line.match(/^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,.\d]*)\s+(DEBUG|INFO|WARN(?:ING)?|ERROR|ERR|DBG|INF)\s+([\s\S]*)$/i);
    // Lamp: [0be]2026-04-13 17:55:13 [0beDEBUG] message %key=value
    const goMatch = line.match(/^(\[\w+\]\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(\[\w+(?:DEBUG|INFO|WARN|ERROR)\])\s+([\s\S]*)$/i);

    if (!pyMatch && !goMatch) return line;

    const ts = pyMatch ? pyMatch[1] : goMatch![1];
    const lvl = pyMatch ? pyMatch[2] : goMatch![2];
    let rest = pyMatch ? pyMatch[3] : goMatch![3];

    // Split message from %key=value metadata
    const metaIdx = rest.search(/\s%\w+=/);
    let msg = rest;
    let meta = "";
    if (metaIdx >= 0) {
      msg = rest.slice(0, metaIdx);
      meta = rest.slice(metaIdx);
    }

    return (
      <>
        <span style={{ opacity: 0.35 }}>{ts}</span>
        {" "}
        <span style={{ fontWeight: 700 }}>{lvl}</span>
        {" "}
        {msg}
        {meta && <span style={{ opacity: 0.3 }}>{meta}</span>}
      </>
    );
  };

  const btnStyle: React.CSSProperties = {
    fontSize: 10, padding: "3px 8px", borderRadius: 5,
    background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
    color: "var(--lm-text-dim)", cursor: "pointer", fontWeight: 600,
  };

  return (
    <div style={{ ...S.card, flex: 1, minHeight: 0, padding: 0, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "8px 12px", borderBottom: "1px solid var(--lm-border)", display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
        <span style={{ ...S.cardLabel, marginBottom: 0, fontSize: 12 }}>{label}</span>
        <button onClick={fetchLines} style={btnStyle}>↻</button>
        <button
          onClick={() => setPaused((p) => !p)}
          style={{
            ...btnStyle,
            background: paused ? "var(--lm-amber-dim)" : "var(--lm-surface)",
            color: paused ? "var(--lm-amber)" : "var(--lm-text-dim)",
          }}
        >
          {paused ? "▶" : "⏸"}
        </button>
        <select
          value={lastN}
          onChange={(e) => setLastN(Number(e.target.value))}
          style={{ fontSize: 10, padding: "3px 6px", borderRadius: 5, background: "var(--lm-surface)", border: "1px solid var(--lm-border)", color: "var(--lm-text)" }}
        >
          {[100, 200, 500, 1000].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <span style={{ width: 1, height: 16, background: "var(--lm-border)", margin: "0 2px" }} />
        <select
          value={level}
          onChange={(e) => { const v = e.target.value as LogLevel; setLevel(v); onFilterChange(source, filter, v); }}
          style={{
            fontSize: 10, padding: "3px 6px", borderRadius: 5,
            background: level !== "ALL" ? "var(--lm-amber-dim)" : "var(--lm-surface)",
            border: "1px solid var(--lm-border)",
            color: level !== "ALL" ? "var(--lm-amber)" : "var(--lm-text)",
            fontWeight: level !== "ALL" ? 700 : 400,
          }}
        >
          {LOG_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <input
          type="text"
          value={filter}
          onChange={(e) => { setFilter(e.target.value); onFilterChange(source, e.target.value, level); }}
          placeholder="grep..."
          style={{
            fontSize: 10, padding: "3px 8px", borderRadius: 5, width: 120,
            background: filter ? "var(--lm-amber-dim)" : "var(--lm-surface)",
            border: `1px solid ${filter ? "var(--lm-amber)" : "var(--lm-border)"}`,
            color: "var(--lm-text)", fontFamily: "monospace",
            outline: "none",
          }}
        />
        {filter && (
          <button onClick={() => { setFilter(""); onFilterChange(source, "", level); }} style={{ ...btnStyle, padding: "3px 6px" }}>✕</button>
        )}
        <button
          onClick={() => {
            const text = (filtered.length ? filtered : lines).join("\n");
            const blob = new Blob([text], { type: "text/plain" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${source}-${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
            a.click();
            URL.revokeObjectURL(url);
          }}
          title="Download visible lines as .log"
          style={btnStyle}
        >↓</button>
        <button onClick={() => setLines([])} style={btnStyle}>Clear</button>
        <label style={{ marginLeft: "auto", fontSize: 9, color: "var(--lm-text-muted)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer", userSelect: "none" }}>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
            style={{ width: 11, height: 11, accentColor: "var(--lm-amber)", cursor: "pointer" }}
          />
          Auto-scroll
        </label>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)" }}>
          {loading ? "Loading..." : error ? error : filtered.length !== lines.length ? `${filtered.length}/${lines.length}` : `${lines.length} lines`}
        </span>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{
          flex: 1, overflowY: "auto", padding: "6px 0",
          fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
          fontSize: 10.5, lineHeight: 1.55,
          whiteSpace: "pre-wrap" as const,
          overflowWrap: "anywhere" as const,
        }}
        className="lm-hide-scroll"
      >
        {filtered.length === 0 ? (
          <div style={{ padding: "12px 14px", color: "var(--lm-text-muted)", fontSize: 11 }}>
            {error ? error : filter || level !== "ALL" ? "No matching lines." : `No log lines from ${label} yet.`}
          </div>
        ) : (
          filtered.map((line, i) => {
            const ll = detectLevel(line);
            return (
              <div key={i} style={{
                padding: "3px 12px",
                color: levelColor[ll],
                borderLeft: `2px solid ${ll === "ERROR" ? "#f87171" : ll === "WARN" ? "#fbbf24" : "transparent"}`,
                background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.02)",
              }}>
                {highlightLine(line)}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

const STORAGE_KEY = "lm-logs-state";

function loadLogState(): { active: LogSource; filters: Record<string, { filter: string; level: LogLevel }> } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        active: LOG_SOURCES.some((s) => s.id === parsed.active) ? parsed.active : "openclaw",
        filters: parsed.filters ?? {},
      };
    }
  } catch {}
  return { active: "openclaw", filters: {} };
}

// saveLogState is debounced so per-keystroke filter edits don't hammer localStorage.
let _saveTimer: number | null = null;
function saveLogState(active: LogSource, filters: Record<string, { filter: string; level: LogLevel }>) {
  if (_saveTimer != null) clearTimeout(_saveTimer);
  _saveTimer = window.setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ active, filters }));
    } catch {}
  }, 250);
}

export function LogsSection() {
  const [saved] = useState(loadLogState);
  const [active, setActive] = useState<LogSource>(saved.active);
  const [filters, setFilters] = useState<Record<string, { filter: string; level: LogLevel }>>(saved.filters);

  const src = LOG_SOURCES.find((s) => s.id === active)!;

  const handleTabChange = (id: LogSource) => {
    setActive(id);
    saveLogState(id, filters);
  };

  const handleFilterChange = (source: LogSource, filter: string, level: LogLevel) => {
    setFilters((prev) => {
      const next = { ...prev, [source]: { filter, level } };
      saveLogState(active, next);
      return next;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0, height: "100%" }}>
      <div style={{ display: "flex", gap: 4, padding: "0 0 8px 0", flexShrink: 0 }}>
        {LOG_SOURCES.map((s) => (
          <button
            key={s.id}
            onClick={() => handleTabChange(s.id)}
            style={{
              fontSize: 11, padding: "4px 12px", borderRadius: 6, cursor: "pointer",
              border: active === s.id ? `1px solid ${s.color}` : "1px solid var(--lm-border)",
              background: active === s.id ? `${s.color}22` : "var(--lm-surface)",
              color: active === s.id ? s.color : "var(--lm-text-dim)",
              fontWeight: active === s.id ? 700 : 400,
              transition: "all 0.15s",
            }}
          >
            <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: s.color, marginRight: 5, verticalAlign: "middle" }} />
            {s.label}
          </button>
        ))}
      </div>
      <LogPanel
        key={active}
        source={src.id}
        label={src.label}
        color={src.color}
        initialFilter={filters[src.id]?.filter ?? ""}
        initialLevel={filters[src.id]?.level ?? "ALL"}
        onFilterChange={handleFilterChange}
      />
    </div>
  );
}
