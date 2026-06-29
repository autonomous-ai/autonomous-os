import { useState } from "react";
import { createPortal } from "react-dom";
import {
  Megaphone, Timer, PauseCircle, Mic, Armchair, Ban, MessageSquare,
  Volume2, TriangleAlert, Moon, Lightbulb, X, Workflow, Circle,
} from "lucide-react";
import type { Turn } from "./types";
import { TYPE_LUCIDE, TURN_INPUT_FALLBACK } from "./types";
import { HW } from "../types";
import { useTheme } from "@/lib/useTheme";
import { turnIO, turnTokenStats, turnCurrentUser } from "./helpers";
import { PoseBucketModal } from "./PoseBucketModal";
import { UserAvatar } from "./UserAvatar";

export function TurnBadge({ turn, pairTint, userPhotos, onViewPipeline }: {
  turn: Turn;
  pairTint?: string;
  userPhotos?: Record<string, string>;
  onViewPipeline?: () => void;
}) {
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [bucketOpen, setBucketOpen] = useState(false);
  const [, , themeClass] = useTheme();
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
  const SourceIcon = TYPE_LUCIDE[turn.type] ?? Circle;
  // Source icon takes the turn's source-category color (mic / cam / channel /
  // web / cron / system) instead of a dim grey, so it stands out and doubles
  // as a quick at-a-glance source cue. Falls back to teal for unmapped types.
  const sourceColor =
    /voice|sound|speech_emotion/.test(turn.type) ? "var(--lm-purple)"        // mic
    : /motion|presence|light|emotion|pose|environment/.test(turn.type) ? "var(--lm-blue)" // cam
    : /telegram|discord|slack|wechat|channel/.test(turn.type) ? "var(--lm-cyan)"          // channel
    : /web_chat/.test(turn.type) ? "var(--lm-teal)"                          // web
    : /cron/.test(turn.type) ? "var(--lm-amber)"                            // cron
    : /system|schedule|music/.test(turn.type) ? "var(--lm-text-dim)"        // system
    : /touch|head_pat/.test(turn.type) ? "var(--lm-green)"                  // button
    : "var(--lm-teal)";
  const { input, output, hwOutput, snapshotUrls, audioUrls, poseBucket } = turnIO(turn);
  // When a motion.activity turn folded in a posture nudge, append the
  // first two worst pose snapshots to the existing strip (capped to 3
  // tiles total including the motion frame). The remaining samples are
  // surfaced via the "Load more" → PoseBucketModal popup.
  const baseSnaps: string[] = [...snapshotUrls];
  let extraStrip: string[] = [];
  if (poseBucket && poseBucket.files.length > 0) {
    extraStrip = poseBucket.files.slice(0, 2).map(
      (f) => `${HW}/sensing/pose-bucket/${encodeURIComponent(poseBucket.id)}/img/${encodeURIComponent(f)}`,
    );
  }
  // 3-tile cap: keep at most 1 baseline snapshot + up to 2 bucket worst.
  // When baseSnaps already has ≥3 we leave them alone (other event types).
  const stripUrls: string[] = poseBucket
    ? [...baseSnaps.slice(0, 1), ...extraStrip].slice(0, 3)
    : baseSnaps;
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
  const pathLabel = turn.path === "agent" ? "Agent" : turn.path === "dropped" ? "dropped" : turn.path === "queued" ? "queued" : turn.path;

  return (
    <div data-region="FLOW_TURN_CARD" data-turn-id={turn.id} data-turn-type={turn.type} style={{
      padding: "8px 10px",
      borderRadius: 8,
      background: pairTint || "var(--lm-surface)",
      border: "1px solid var(--lm-border)",
      fontSize: 11,
      cursor: "default",
    }}>
      {/* Card header: the badge row (icon + type + path + status + user +
          timing) and the time/id meta line — the part shown in the collapsed
          turn card. Marked as a region so it can be targeted/inspected like
          the other FLOW_* regions. */}
      <div data-region="FLOW_TURN_CARD_HEADER">
      {/* Row 1: source icon + type + path + status tag + duration */}
      <div data-region="FLOW_TURN_CARD_BADGES" style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 4, flexWrap: "wrap", rowGap: 3 }}>
        <span className="lm-turn-src" style={{
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          width: 18, height: 18, borderRadius: 5, flexShrink: 0,
          color: sourceColor,
          background: `color-mix(in srgb, ${sourceColor} 16%, transparent)`,
        }}>
          <SourceIcon size={12} strokeWidth={2.25} />
        </span>
        <span style={{
          fontSize: 10, fontWeight: 700, color: "var(--lm-text)",
          textTransform: "uppercase" as const,
        }}>{turn.type}</span>
        <span style={{
          fontSize: 8, padding: "1px 5px", borderRadius: 3,
          background: `${pathColor}18`, color: pathColor, fontWeight: 700,
        }}>{pathLabel}</span>
        <span
          className={statusLabel === "ACTIVE" ? "lm-turn-active" : undefined}
          style={{
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: `${statusColor}18`, color: statusColor, fontWeight: 700,
            textTransform: "uppercase" as const,
          }}
        >{statusLabel}</span>
        {hasBroadcast && (
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 3,
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: "var(--lm-red-dim)", color: "var(--lm-red)", fontWeight: 700,
          }}><Megaphone size={9} strokeWidth={2.5} /> BROADCAST</span>
        )}
        {currentUser && (() => {
          const isUnknown = currentUser === "unknown";
          const color = isUnknown ? "var(--lm-text-muted)" : "var(--lm-teal)";
          const photo = !isUnknown ? userPhotos?.[currentUser] : undefined;
          return (
            <span
              title={isUnknown ? "Current user: stranger/unknown" : `Current user: ${currentUser}`}
              style={{
                display: "inline-flex", alignItems: "center", gap: 4,
                fontSize: 8, padding: "1px 5px 1px 2px", borderRadius: 999,
                background: `${color}18`, color, fontWeight: 700,
                textTransform: "capitalize" as const,
              }}
            >
              <UserAvatar user={currentUser} photo={photo} size={13} color={color} />
              {currentUser}
            </span>
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
            display: "inline-flex", alignItems: "center", gap: 3,
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: `${durColor}18`, color: durColor, fontWeight: 700,
          }}><Timer size={9} strokeWidth={2.5} /> {label}</span>;
        })()}
        {typeof turn.queuedForMs === "number" && turn.queuedForMs > 0 && (() => {
          const ms = turn.queuedForMs;
          const label = ms >= 60_000 ? `${(ms / 60_000).toFixed(1)}m`
            : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s`
            : `${ms}ms`;
          return <span style={{
            display: "inline-flex", alignItems: "center", gap: 3,
            fontSize: 8, padding: "1px 5px", borderRadius: 3,
            background: "var(--lm-amber)18", color: "var(--lm-amber)", fontWeight: 700,
          }} title="queued waiting for agent before processing"><PauseCircle size={9} strokeWidth={2.5} /> queued {label}</span>;
        })()}
      </div>

      {/* Meta line: time + traceable id on one dim mono row. The id is a
          debug detail (rarely read), so it's truncated to the tail and the
          full value lives in `title` — keeping it from dominating the card. */}
      <div data-region="FLOW_TURN_CARD_META" style={{
        fontSize: 8.5, color: "var(--lm-text-muted)", fontFamily: "monospace",
        marginBottom: 5, display: "flex", alignItems: "center", gap: 6,
        whiteSpace: "nowrap", overflow: "hidden",
      }}>
        <span style={{ color: "var(--lm-text-dim)", flexShrink: 0 }}>{formatTurnTime(turn.startTime)}</span>
        <span style={{ opacity: 0.4, flexShrink: 0 }}>·</span>
        <span
          title={`${turn.id.startsWith("device-") ? "device id" : "agent uuid"}: ${turn.id}`}
          style={{ overflow: "hidden", textOverflow: "ellipsis" }}
        >
          {turn.id.startsWith("device-") ? "device" : "uuid"}:…{turn.id.slice(-8)}
        </span>
      </div>
      </div>
      {/* Input — primary content, the loudest text in the card. */}
      <div style={{
        fontSize: 12, color: "var(--lm-text)", marginBottom: 4,
        overflowWrap: "anywhere" as const, lineHeight: 1.5,
      }}>
        <span style={{
          fontSize: 8, fontWeight: 700, letterSpacing: "0.06em",
          color: "var(--lm-teal)", marginRight: 6,
          padding: "1px 4px", borderRadius: 3,
          background: "color-mix(in srgb, var(--lm-teal) 15%, transparent)",
          verticalAlign: "1px",
        }}>IN</span>
        {input || TURN_INPUT_FALLBACK}
      </div>
      {stripUrls.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 4 }}>
          {stripUrls.map((url, i) => (
            <img
              key={i}
              src={url}
              alt={`snapshot ${i + 1}`}
              onClick={() => setLightboxUrl(url)}
              style={{
                width: stripUrls.length === 1 ? "100%" : stripUrls.length === 2 ? "48%" : "32%",
                maxWidth: 180, borderRadius: 6,
                border: "1px solid var(--lm-border)", opacity: 0.9,
                cursor: "pointer",
              }}
            />
          ))}
        </div>
      )}
      {audioUrls.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 4 }}>
          {audioUrls.map((url, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <span
                title="Debug clip that produced this emotion — not sent to the LLM"
                style={{
                  display: "inline-flex", alignItems: "center", gap: 3,
                  fontSize: 9, color: "var(--lm-text-dim)", whiteSpace: "nowrap",
                }}
              >
                <Mic size={10} strokeWidth={2} /> debug
              </span>
              <audio
                controls
                preload="none"
                src={url}
                onClick={(e) => e.stopPropagation()}
                style={{ width: "100%", height: 28 }}
              />
            </div>
          ))}
        </div>
      )}
      {poseBucket && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setBucketOpen(true); }}
          title="Open pose bucket viewer"
          style={{
            marginBottom: 6,
            padding: "3px 8px",
            borderRadius: 4,
            background: "transparent",
            border: "1px solid var(--lm-purple)55",
            color: "var(--lm-purple)",
            cursor: "pointer",
            fontSize: 9,
            fontWeight: 700,
            letterSpacing: 0.3,
            display: "inline-flex", alignItems: "center", gap: 4,
          }}
        >
          <Armchair size={11} strokeWidth={2.25} /> LOAD MORE · pose bucket {poseBucket.id}
          {poseBucket.files.length > 0 ? ` · ${poseBucket.files.length} worst` : ""}
        </button>
      )}
      {bucketOpen && poseBucket && (
        <PoseBucketModal bucketId={poseBucket.id} onClose={() => setBucketOpen(false)} />
      )}
      {lightboxUrl && createPortal(
        // Portalled to <body> so the overlay's position:fixed anchors to the
        // viewport, not to this card. The card has a transform/animation
        // (hover lift + entrance), which would otherwise become the fixed
        // containing block and let the lightbox overflow on top of the page
        // instead of covering it. The `lm-root ${themeClass}` re-scope keeps
        // the --lm-* tokens resolving outside the monitor root.
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
      {/* Row 3: output — TTS or no reply */}
      {output === "[no reply]" ? (
        <div style={{
          fontSize: 11.5, color: "var(--lm-text-muted)", marginBottom: 2,
          lineHeight: 1.45, fontStyle: "italic",
          display: "flex", alignItems: "center", gap: 5,
        }}>
          <Ban size={12} strokeWidth={2} style={{ flexShrink: 0 }} /> no reply — agent decided to do nothing
        </div>
      ) : output ? (
        <div style={{
          fontSize: 12, color: "var(--lm-text-dim)", marginBottom: 2,
          overflowWrap: "anywhere" as const, lineHeight: 1.5,
        }}>
          <span style={{
            color: "var(--lm-purple)", fontWeight: 600, marginRight: 6,
            display: "inline-flex", alignItems: "center", gap: 3, verticalAlign: "text-bottom",
          }}>
            {["telegram","discord","slack","wechat","channel"].includes(turn.type)
              ? <MessageSquare size={12} strokeWidth={2} />
              : <><Volume2 size={12} strokeWidth={2} /> TTS</>}
          </span>
          {output}
        </div>
      ) : turn.path === "dropped" ? (
        <div style={{
          fontSize: 11.5, color: "var(--lm-red)", marginBottom: 2,
          lineHeight: 1.45, fontStyle: "italic",
          display: "flex", alignItems: "center", gap: 5,
        }}>
          <PauseCircle size={12} strokeWidth={2} style={{ flexShrink: 0 }} /> dropped — agent was busy
        </div>
      ) : turn.path === "queued" ? (
        <div style={{
          fontSize: 11.5, color: "var(--lm-amber)", marginBottom: 2,
          lineHeight: 1.45, fontStyle: "italic",
          display: "flex", alignItems: "center", gap: 5,
        }}>
          <PauseCircle size={12} strokeWidth={2} style={{ flexShrink: 0 }} /> queued — agent busy, will replay when idle
        </div>
      ) : hasEmptyFinalNoLifecycle ? (
        <div
          title={
            "Agent sent state:final with empty message for this run_id, and never opened a lifecycle for it.\n\n" +
            "To find the likely paired turn:\n" +
            "  • Scan ±10s in the list for an 'agent uuid' turn with matching input text.\n" +
            "  • If found → Agent likely re-fired this message under its own UUID (source:\"channel\"), or merged it into that concurrent turn. The actual reply lives there.\n" +
            "  • If no UUID turn with matching input → the message was steered into an already-running concurrent turn, or dropped silently.\n\n" +
            "Adjacent paired turns are tinted purple in the list."
          }
          style={{
            fontSize: 11.5, color: "var(--lm-red)", marginBottom: 2,
            overflowWrap: "anywhere" as const, lineHeight: 1.45,
            fontWeight: 700, cursor: "help",
            display: "flex", alignItems: "center", gap: 5,
          }}
        >
          <TriangleAlert size={12} strokeWidth={2.25} style={{ flexShrink: 0 }} /> Agent closed stream · no message · no lifecycle
        </div>
      ) : turn.status === "done" ? (
        <div style={{
          fontSize: 11.5, color: "var(--lm-text-muted)", marginBottom: 2,
          lineHeight: 1.45, fontStyle: "italic",
          display: "flex", alignItems: "center", gap: 5,
        }}>
          <Moon size={12} strokeWidth={2} style={{ flexShrink: 0 }} /> no output — agent processed silently
        </div>
      ) : null}
      {/* Row 3b: output — Hardware actions */}
      {hwOutput && (
        <div style={{
          fontSize: 11.5, color: "var(--lm-text-dim)",
          overflowWrap: "anywhere" as const, lineHeight: 1.45,
        }}>
          <span style={{
            color: "var(--lm-amber)", fontWeight: 600, marginRight: 5,
            display: "inline-flex", alignItems: "center", gap: 3, verticalAlign: "text-bottom",
          }}><Lightbulb size={12} strokeWidth={2} /> HW</span>
          {hwOutput}
        </div>
      )}
      {/* Footer meta: events + tokens on one quiet hairline-topped row. Tokens
          were a loud red box that read like an error; now a compact inline
          summary, with the full cache/billed breakdown in `title` on hover. */}
      <div data-region="FLOW_TURN_CARD_FOOTER" style={{
        fontSize: 10.5, color: "var(--lm-text-dim)", marginTop: 7, paddingTop: 6,
        borderTop: "1px solid var(--lm-border)",
        display: "flex", gap: 9, alignItems: "center", flexWrap: "wrap",
        fontFamily: "monospace",
      }}>
        <span style={{ fontWeight: 600 }}>{turn.events.length} events</span>
        {tokenStats && (() => {
          const billed = tokenStats.inTok + tokenStats.cacheWrite
            + Math.round(tokenStats.cacheRead * 0.1) + tokenStats.outTok;
          const title = `Tokens — in ${fmtToken(tokenStats.inTok)} / out ${fmtToken(tokenStats.outTok)} · total ${fmtToken(tokenStats.total)}`
            + ((tokenStats.cacheRead || tokenStats.cacheWrite)
              ? `\nCache read ${fmtToken(tokenStats.cacheRead)} / write ${fmtToken(tokenStats.cacheWrite)} · billed ~${fmtToken(billed)}`
              : "");
          return (
            <span title={title} style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
              <span style={{ opacity: 0.4 }}>·</span>
              <span>
                <span style={{ color: "var(--lm-teal)", fontWeight: 700 }}>↓{fmtToken(tokenStats.inTok)}</span>
                {" "}
                <span style={{ color: "var(--lm-amber)", fontWeight: 700 }}>↑{fmtToken(tokenStats.outTok)}</span>
                {" "}
                <span style={{ color: "var(--lm-text-muted)" }}>tokens</span>
              </span>
            </span>
          );
        })()}
      </div>
      {onViewPipeline && (
        <button
          type="button"
          className="lm-view-pipeline-btn"
          onClick={(e) => { e.stopPropagation(); onViewPipeline(); }}
          style={{
            // NOTE: do NOT set `display` here — visibility is controlled by
            // `.lm-view-pipeline-btn` (none on desktop, flex on mobile). An
            // inline `display` would override that non-!important rule and leak
            // the mobile-only button onto desktop. The mobile CSS sets
            // `display: flex`, which already aligns the icon + label.
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
        ><Workflow size={13} strokeWidth={2} /> View pipeline</button>
      )}
    </div>
  );
}
