import { useState } from "react";
import type { Turn } from "./types";
import { SOURCE_ICON, TURN_INPUT_FALLBACK } from "./types";
import { turnIO, turnTokenStats, turnCurrentUser } from "./helpers";

export function TurnBadge({ turn, pairTint, onViewPipeline }: { turn: Turn; pairTint?: string; onViewPipeline?: () => void }) {
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const formatTurnTime = (iso: string): string => {
    const date = new Date(iso);
    const diffMs = Date.now() - date.getTime();
    if (diffMs >= 0 && diffMs < 30 * 60 * 1000) {
      const diffSec = Math.floor(diffMs / 1000);
      if (diffSec < 60) return `${diffSec}s ago`;
      const diffMin = Math.floor(diffSec / 60);
      return `${diffMin} min ago`;
    }
    const m = iso.match(/T(\d{2}:\d{2}:\d{2})/);
    return (m?.[1] ?? iso).trim();
  };

  const pathColor = turn.path === "dropped" ? "var(--lm-red)"
    : turn.path === "queued" ? "var(--lm-amber)"
    : turn.path === "local" ? "var(--lm-green)"
    : turn.path === "agent" ? "var(--lm-blue)"
    : "var(--lm-text-muted)";
  const statusColor = turn.status === "done" ? "var(--lm-green)"
    : turn.status === "error" ? "var(--lm-red)"
    : "var(--lm-amber)";
  const icon = SOURCE_ICON[turn.type] ?? SOURCE_ICON.unknown;
  const { input, output, hwOutput, snapshotUrls } = turnIO(turn);
  const tokenStats = turnTokenStats(turn);
  const currentUser = turnCurrentUser(turn);
  const hasBroadcast = turn.events.some((ev) =>
    ev.type === "flow_event" && (ev.detail as Record<string, any>)?.node === "telegram_alert_broadcast"
  );
  const fmtToken = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  const statusLabel = turn.status === "done"
    ? "DONE"
    : turn.status === "error"
      ? "ERROR"
      : "ACTIVE";
  const hasEmptyFinalNoLifecycle = turn.events.some((ev) =>
    ev.type === "flow_event" && (
      (ev.detail as Record<string, any>)?.node === "chat_final_empty" ||
      (ev.detail as Record<string, any>)?.node === "turn_steered"
    )
  );
  const pathLabel = turn.path === "agent" ? "OpenClaw" : turn.path === "dropped" ? "dropped" : turn.path === "queued" ? "queued" : turn.path;

  return (
    <div style={{
      padding: "8px 10px",
      borderRadius: 8,
      background: pairTint || "var(--lm-surface)",
      border: "1px solid var(--lm-border)",
      fontSize: 11,
      cursor: "default",
    }}>
      {/* Row 1: source icon + type + path + status tag + duration */}
      <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 4, flexWrap: "wrap", rowGap: 3 }}>
        <span style={{ fontSize: 14, lineHeight: 1 }}>{icon}</span>
        <span style={{
          fontSize: 10, fontWeight: 700, color: "var(--lm-text)",
          textTransform: "uppercase" as const,
        }}>{turn.type}</span>
        <span style={{
          fontSize: 8, padding: "1px 5px", borderRadius: 3,
          background: `${pathColor}18`, color: pathColor, fontWeight: 700,
        }}>{pathLabel}</span>
        <span style={{
          fontSize: 8, padding: "1px 5px", borderRadius: 3,
          background: `${statusColor}18`, color: statusColor, fontWeight: 700,
          textTransform: "uppercase" as const,
        }}>{statusLabel}</span>
        {hasBroadcast && (
          <span style={{
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: "#e5393518", color: "#e53935", fontWeight: 700,
          }}>📢 BROADCAST</span>
        )}
        {currentUser && (() => {
          const isUnknown = currentUser === "unknown";
          const color = isUnknown ? "var(--lm-text-muted)" : "var(--lm-teal)";
          return (
            <span
              title={isUnknown ? "Current user: stranger/unknown" : `Current user: ${currentUser}`}
              style={{
                fontSize: 8, padding: "1px 5px", borderRadius: 3,
                background: `${color}18`, color, fontWeight: 700,
              }}
            >👤 {currentUser}</span>
          );
        })()}
        {turn.endTime && (() => {
          const ms = new Date(turn.endTime).getTime() - new Date(turn.startTime).getTime();
          if (!Number.isFinite(ms) || ms < 0) return null;
          const label = ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
            : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s`
            : `${ms}ms`;
          const durColor = ms > 15_000 ? "var(--lm-red)" : ms > 5_000 ? "var(--lm-amber)" : "var(--lm-green)";
          return <span style={{
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: `${durColor}18`, color: durColor, fontWeight: 700,
          }}>⏱ {label}</span>;
        })()}
        {typeof turn.queuedForMs === "number" && turn.queuedForMs > 0 && (() => {
          const ms = turn.queuedForMs;
          const label = ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
            : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s`
            : `${ms}ms`;
          return <span style={{
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: "var(--lm-amber)18", color: "var(--lm-amber)", fontWeight: 700,
          }} title="queued waiting for agent before processing">⏸ queued {label}</span>;
        })()}
      </div>

      {/* Row 2: time */}
      <div style={{
        fontSize: 8,
        color: "var(--lm-text)",
        fontFamily: "monospace",
        marginBottom: 3,
        opacity: 0.95,
      }}>
        {formatTurnTime(turn.startTime)}
      </div>
      {/* Turn ID for tracing — label by ID origin (Lumi-emitted vs OpenClaw-assigned UUID) */}
      <div style={{ fontSize: 8, color: "var(--lm-text)", fontFamily: "monospace", marginBottom: 3, opacity: 0.7 }}>
        {turn.id.startsWith("lumi-") ? "lumi id" : "openclaw uuid"}: {turn.id}
      </div>
      {/* Row 2: input */}
      <div style={{
        fontSize: 10, color: "var(--lm-text-dim)", marginBottom: 3,
        wordBreak: "break-word" as const, lineHeight: 1.4,
      }}>
        <span style={{ color: "var(--lm-teal)", fontWeight: 600, marginRight: 4 }}>IN</span>
        {input || TURN_INPUT_FALLBACK}
      </div>
      {snapshotUrls.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 4 }}>
          {snapshotUrls.map((url, i) => (
            <img
              key={i}
              src={url}
              alt={`snapshot ${i + 1}`}
              onClick={() => setLightboxUrl(url)}
              style={{
                width: snapshotUrls.length === 1 ? "100%" : "48%",
                maxWidth: 180, borderRadius: 6,
                border: "1px solid var(--lm-border)", opacity: 0.9,
                cursor: "pointer",
              }}
            />
          ))}
        </div>
      )}
      {lightboxUrl && (
        <div
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
            style={{
              position: "absolute", top: 16, right: 16,
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", fontSize: 20, width: 36, height: 36,
              borderRadius: "50%", cursor: "pointer",
            }}
          >
            ✕
          </button>
          <img
            src={lightboxUrl}
            onClick={(e) => e.stopPropagation()}
            style={{ width: "85vw", height: "85vh", objectFit: "contain", borderRadius: 8, cursor: "default" }}
          />
        </div>
      )}
      {/* Row 3: output — TTS or no reply */}
      {output === "[no reply]" ? (
        <div style={{
          fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 2,
          wordBreak: "break-word" as const, lineHeight: 1.4, fontStyle: "italic",
        }}>
          🚫 no reply — agent decided to do nothing
        </div>
      ) : output ? (
        <div style={{
          fontSize: 10, color: "var(--lm-text-dim)", marginBottom: 2,
          wordBreak: "break-word" as const, lineHeight: 1.4,
        }}>
          <span style={{ color: "var(--lm-purple)", fontWeight: 600, marginRight: 4 }}>{["telegram","discord","slack","wechat","channel"].includes(turn.type) ? "💬" : "TTS 🔊"}</span>
          {output}
        </div>
      ) : turn.path === "dropped" ? (
        <div style={{
          fontSize: 10, color: "var(--lm-red)", marginBottom: 2,
          wordBreak: "break-word" as const, lineHeight: 1.4, fontStyle: "italic",
        }}>
          ⏸ dropped — agent was busy
        </div>
      ) : turn.path === "queued" ? (
        <div style={{
          fontSize: 10, color: "var(--lm-amber)", marginBottom: 2,
          wordBreak: "break-word" as const, lineHeight: 1.4, fontStyle: "italic",
        }}>
          ⏸ queued — agent busy, will replay when idle
        </div>
      ) : hasEmptyFinalNoLifecycle ? (
        <div
          title={
            "OpenClaw sent state:final with empty message for this Lumi run_id, and never opened a lifecycle for it.\n\n" +
            "To find the likely paired turn:\n" +
            "  • Scan ±10s in the list for an 'openclaw uuid' turn with matching input text.\n" +
            "  • If found → OpenClaw likely re-fired this message under its own UUID (source:\"channel\"), or merged it into that concurrent turn. The actual reply lives there.\n" +
            "  • If no UUID turn with matching input → the message was steered into an already-running concurrent turn, or dropped silently.\n\n" +
            "Adjacent paired turns are tinted purple in the list."
          }
          style={{
            fontSize: 10, color: "var(--lm-red)", marginBottom: 2,
            wordBreak: "break-word" as const, lineHeight: 1.4,
            fontWeight: 700, cursor: "help",
          }}
        >
          ⚠ OpenClaw closed stream · no message · no lifecycle
        </div>
      ) : turn.status === "done" ? (
        <div style={{
          fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 2,
          wordBreak: "break-word" as const, lineHeight: 1.4, fontStyle: "italic",
        }}>
          💤 no output — agent processed silently
        </div>
      ) : null}
      {/* Row 3b: output — Hardware actions */}
      {hwOutput && (
        <div style={{
          fontSize: 10, color: "var(--lm-text-dim)",
          wordBreak: "break-word" as const, lineHeight: 1.4,
        }}>
          <span style={{ color: "var(--lm-amber)", fontWeight: 600, marginRight: 4 }}>HW 💡</span>
          {hwOutput}
        </div>
      )}
      {tokenStats && (
        <div style={{
          marginTop: 6,
          padding: "5px 7px",
          borderRadius: 6,
          border: "1px solid rgba(248,113,113,0.55)",
          background: "rgba(248,113,113,0.14)",
          fontSize: 9,
          fontFamily: "monospace",
          lineHeight: 1.6,
        }}>
          <div>
            <span style={{ color: "var(--lm-text)" }}>Tokens </span>
            <span style={{ color: "var(--lm-teal)" }}>in </span>
            <span style={{ color: "var(--lm-text-dim)", fontWeight: 600 }}>{fmtToken(tokenStats.inTok)}</span>
            <span style={{ color: "var(--lm-text)" }}> / </span>
            <span style={{ color: "var(--lm-amber)" }}>out </span>
            <span style={{ color: "var(--lm-text-dim)", fontWeight: 600 }}>{fmtToken(tokenStats.outTok)}</span>
          </div>
          <div>
            <span style={{ color: "var(--lm-text)" }}>Total </span>
            <span style={{ color: "var(--lm-text-dim)", fontWeight: 600 }}>{fmtToken(tokenStats.total)}</span>
          </div>
          {(tokenStats.cacheRead || tokenStats.cacheWrite) ? (
            <>
              <div>
                <span style={{ color: "var(--lm-text)" }}>Cache read </span>
                <span style={{ color: "var(--lm-teal)", fontWeight: 600 }}>{fmtToken(tokenStats.cacheRead)}</span>
                <span style={{ color: "var(--lm-text)" }}> / write </span>
                <span style={{ color: "var(--lm-amber)", fontWeight: 600 }}>{fmtToken(tokenStats.cacheWrite)}</span>
              </div>
              <div>
                <span style={{ color: "var(--lm-text)" }}>Billed </span>
                <span style={{ color: "var(--lm-purple)", fontWeight: 600 }}>~{fmtToken(tokenStats.inTok + tokenStats.cacheWrite + Math.round(tokenStats.cacheRead * 0.1) + tokenStats.outTok)}</span>
              </div>
            </>
          ) : null}
        </div>
      )}
      {/* Row 4: event count */}
      <div style={{ fontSize: 9, color: "var(--lm-text-muted)", marginTop: 3, display: "flex", gap: 8, alignItems: "center" }}>
        <span>{turn.events.length} events</span>
      </div>
      {onViewPipeline && (
        <button
          type="button"
          className="lm-view-pipeline-btn"
          onClick={(e) => { e.stopPropagation(); onViewPipeline(); }}
          style={{
            marginTop: 8,
            width: "100%",
            padding: "7px 10px",
            borderRadius: 6,
            background: "var(--lm-amber-dim)",
            border: "1px solid var(--lm-amber)",
            color: "var(--lm-amber)",
            cursor: "pointer",
            fontSize: 11,
            fontWeight: 700,
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
          }}
        >⬢ View pipeline</button>
      )}
    </div>
  );
}
