import { useState } from "react";
import { HW } from "./types";
import { usePolling } from "../../hooks/usePolling";
import { S } from "./styles";
import { StatPill } from "./components";

function fmtAgo(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

// Raw per-side ergo data from dlbackend (passed through by lelamp).
interface PoseSide {
  score?: number;
  risk_level?: number;
  body_scores?: {
    upper_arm?: number; upper_arm_angle?: number;
    lower_arm?: number; lower_arm_angle?: number;
    wrist?: number;
    neck?: number;     neck_angle?: number;
    trunk?: number;    trunk_angle?: number;
  };
  skipped_joints?: string[];
}

interface PoseSample {
  ts: number;
  score: number;
  risk_level: number;
  left?: PoseSide;
  right?: PoseSide;
}

interface PoseSummary {
  bad_ratio: number;
  samples: number;
  bad_samples: number;
  window_min: number;
  region_frequency: Record<string, number>;
  dominant_region: string;
  dominant_count: number;
  latest_score: number;
  latest_risk_level: number;
}

interface Perception {
  type: string;
  connected?: boolean;
  last_raw_actions?: string[];
  last_user?: string | null;
  last_sent_emotion?: string | null;
  last_sent_user?: string | null;
  last_detected_emotion?: string | null;
  buffered_snapshots?: number;
  buffered_emotions?: number;
  motion_detected?: boolean;
  emotion_detected?: boolean;
  seconds_since_motion?: number | null;
  seconds_since_detection?: number | null;
  face_present?: boolean;
  faces_count?: number;
  visible?: string[];
  last_person?: string | null;
  last_seen_seconds_ago?: number | null;
  enrolled_count?: number;
  stranger_count?: number;
  level?: number;
  seconds_since_check?: number | null;
  occurrence_count?: number;
  echo_suppression?: boolean;
  // Pose perception (added with the silent-sampler refactor).
  ergo_score?: number | null;
  ergo_risk_level?: number | null;
  seconds_since_sample?: number | null;
  samples_in_buffer?: number;
  samples_until_gate?: number;
  window_samples?: number;
  sample_interval_s?: number;
  bad_ratio_threshold?: number;
  summary?: PoseSummary | null;
  samples?: PoseSample[];
}

interface SensingData {
  running: boolean;
  poll_interval: number;
  last_event_seconds_ago: Record<string, number>;
  perceptions: Perception[];
  presence: {
    state: string;
    enabled: boolean;
    seconds_since_motion: number;
    idle_timeout: number;
    away_timeout: number;
  };
}

// Status pill used in card headers. Color tier carries quick health signal.
function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span style={{
      fontSize: 10, padding: "2px 7px", borderRadius: 4,
      background: `${color}26`,
      color,
      border: `1px solid ${color}55`,
      fontWeight: 700, letterSpacing: "0.05em",
      textTransform: "uppercase",
    }}>{text}</span>
  );
}

// CardHeader is the uppercase title + pill row shared by every Sensing card.
function CardHeader({ label, pill }: { label: string; pill?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
      <div style={S.cardLabel}>{label}</div>
      {pill}
    </div>
  );
}

// Maps a presence state string to a color so users can scan at a glance.
function presenceColor(state: string): string {
  switch (state) {
    case "active":   return "var(--lm-green)";
    case "idle":     return "var(--lm-amber)";
    case "away":     return "var(--lm-red)";
    default:         return "var(--lm-text-muted)";
  }
}

// Light tier heuristic — sensor returns raw 0-1000ish; rough buckets for UX.
// Color progression dim → bright: muted → blue → teal → green. Amber stays
// reserved for state-warning so it isn't used here.
function lightTier(level: number): { label: string; color: string } {
  if (level < 30)  return { label: "Dark",   color: "var(--lm-text-muted)" };
  if (level < 200) return { label: "Dim",    color: "var(--lm-blue)" };
  if (level < 600) return { label: "Bright", color: "var(--lm-teal)" };
  return                  { label: "Sunlit", color: "var(--lm-green)" };
}

// Risk tier for pose samples. Mirrors the lelamp risk_level enum:
// 0 (no data) / 1 (negligible) / 2 (low) / 3 (medium) / 4 (high).
function poseDotColor(sample: PoseSample): string {
  switch (sample.risk_level) {
    case 4: return "var(--lm-red)";
    case 3: return "var(--lm-amber)";
    case 2: return "var(--lm-teal)";
    case 1: return "var(--lm-green)";
    default: return "var(--lm-text-muted)";
  }
}

function riskName(level: number | null | undefined): string {
  switch (level) {
    case 4: return "high";
    case 3: return "medium";
    case 2: return "low";
    case 1: return "negligible";
    default: return "—";
  }
}

function posePillStatus(pose: Perception): { text: string; color: string } {
  const inBuf = pose.samples_in_buffer ?? 0;
  const win = pose.window_samples ?? 30;
  const summary = pose.summary;
  if (summary && summary.bad_ratio >= (pose.bad_ratio_threshold ?? 0.6)) {
    return { text: `Bad ${Math.round(summary.bad_ratio * 100)}%`, color: "var(--lm-red)" };
  }
  if (summary) {
    return { text: `OK (${Math.round((1 - summary.bad_ratio) * 100)}% clean)`, color: "var(--lm-green)" };
  }
  if (inBuf > 0) {
    return { text: `Filling ${inBuf}/${win}`, color: "var(--lm-amber)" };
  }
  return { text: "Idle", color: "var(--lm-text-muted)" };
}

export function SensingSection() {
  const [data, setData] = useState<SensingData | null>(null);

  usePolling(async (signal) => {
    const r = await fetch(`${HW}/sensing`, { signal }).then((x) => x.json());
    setData(r);
  }, 3000);

  if (!data) return <div style={{ color: "var(--lm-text-muted)", padding: 20 }}>Loading sensing data…</div>;

  const motion = data.perceptions.find((p) => p.type === "motion");
  const emotion = data.perceptions.find((p) => p.type === "emotion");
  const face = data.perceptions.find((p) => p.type === "face");
  const light = data.perceptions.find((p) => p.type === "light_level");
  const sound = data.perceptions.find((p) => p.type === "sound");
  const pose = data.perceptions.find((p) => p.type === "pose");
  const ev = data.last_event_seconds_ago;

  const motionFresh = (motion?.seconds_since_motion ?? Infinity) < 30;
  const faceVisible = (face?.visible?.length ?? 0) > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>

      {/* Row 1: the four primary perception streams. */}
      <div className="lm-grid-4">

        {/* Motion */}
        <div style={S.card}>
          <CardHeader
            label="Motion"
            pill={<Pill
              text={motionFresh ? "Active" : "Quiet"}
              color={motionFresh ? "var(--lm-green)" : "var(--lm-text-muted)"}
            />}
          />
          {motion ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatPill label="Last user"      value={motion.last_user || "unknown"} />
              <StatPill label="Since motion"   value={fmtAgo(motion.seconds_since_motion)} color={motionFresh ? "var(--lm-green)" : undefined} />
              <StatPill label="Buffered snaps" value={motion.buffered_snapshots ?? 0} />
              <StatPill label="Last actions"   value={motion.last_raw_actions?.length ? motion.last_raw_actions.join(", ") : "—"} />
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No data</span>}
        </div>

        {/* Emotion */}
        <div style={S.card}>
          <CardHeader
            label="Emotion"
            pill={emotion?.last_sent_emotion ? (
              <Pill text={emotion.last_sent_emotion} color="var(--lm-amber)" />
            ) : <Pill text="None" color="var(--lm-text-muted)" />}
          />
          {emotion ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatPill label="Sent user"        value={emotion.last_sent_user || "unknown"} />
              <StatPill label="Detecting"        value={emotion.last_detected_emotion ?? "—"} color="var(--lm-amber)" />
              <StatPill label="Since detection"  value={fmtAgo(emotion.seconds_since_detection)} />
              <StatPill label="Buffered"         value={emotion.buffered_emotions ?? 0} />
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No data</span>}
        </div>

        {/* Face */}
        <div style={S.card}>
          <CardHeader
            label="Face"
            pill={<Pill
              text={faceVisible ? `${face?.visible?.length} visible` : "Empty"}
              color={faceVisible ? "var(--lm-green)" : "var(--lm-text-muted)"}
            />}
          />
          {face ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatPill label="Visible now"  value={face.visible?.length ? face.visible.join(", ") : "nobody"} color={faceVisible ? "var(--lm-green)" : undefined} />
              <StatPill label="Last person"  value={face.last_person ?? "—"} />
              <StatPill label="Last seen"    value={fmtAgo(face.last_seen_seconds_ago)} />
              <StatPill label="Enrolled"     value={face.enrolled_count ?? 0} bullet="var(--lm-teal)" />
              <StatPill label="Strangers"    value={face.stranger_count ?? 0} bullet="var(--lm-red)" />
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No data</span>}
        </div>

        {/* Presence */}
        <div style={S.card}>
          <CardHeader
            label="Presence"
            pill={<Pill text={data.presence.state || "—"} color={presenceColor(data.presence.state)} />}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <StatPill label="Sensing"        value={data.presence.enabled ? "On" : "Off"} color={data.presence.enabled ? "var(--lm-green)" : "var(--lm-red)"} />
            <StatPill label="Since motion"   value={fmtAgo(data.presence.seconds_since_motion)} />
            <StatPill label="Idle timeout"   value={`${data.presence.idle_timeout}s`} />
            <StatPill label="Away timeout"   value={`${data.presence.away_timeout}s`} />
          </div>
        </div>
      </div>

      {/* Row 2: secondary signals + diagnostic cards. */}
      <div className="lm-grid-4">

        {/* Light Level */}
        <div style={S.card}>
          {(() => {
            const tier = lightTier(light?.level ?? 0);
            return (
              <>
                <CardHeader
                  label="Light"
                  pill={<Pill text={tier.label} color={tier.color} />}
                />
                {light ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <StatPill label="Level"   value={Math.round(light.level ?? 0)} color={tier.color} />
                    <StatPill label="Checked" value={fmtAgo(light.seconds_since_check)} />
                  </div>
                ) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No data</span>}
              </>
            );
          })()}
        </div>

        {/* Sound */}
        <div style={S.card}>
          <CardHeader
            label="Sound"
            pill={<Pill
              text={sound?.echo_suppression ? "Echo: on" : "Echo: off"}
              color={sound?.echo_suppression ? "var(--lm-teal)" : "var(--lm-text-muted)"}
            />}
          />
          {sound ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatPill label="Occurrences"      value={sound.occurrence_count ?? 0} />
              <StatPill label="Echo suppression" value={sound.echo_suppression ? "On" : "Off"} color={sound.echo_suppression ? "var(--lm-teal)" : undefined} />
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No data</span>}
        </div>

        {/* DL Backend health */}
        <div style={S.card}>
          <CardHeader label="DL Backend" />
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {data.perceptions.filter((p) => p.connected !== undefined).map((p) => (
              <StatPill
                key={p.type}
                label={p.type}
                value={p.connected ? "Connected" : "Down"}
                color={p.connected ? "var(--lm-green)" : "var(--lm-red)"}
                bullet={p.connected ? "var(--lm-green)" : "var(--lm-red)"}
              />
            ))}
          </div>
        </div>

        {/* Last Events */}
        <div style={S.card}>
          <CardHeader
            label="Last Events"
            pill={<Pill text={`${Object.keys(ev).length} types`} color="var(--lm-text-muted)" />}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {Object.entries(ev).length > 0 ? Object.entries(ev).map(([type, sec]) => (
              <StatPill key={type} label={type} value={fmtAgo(sec)} />
            )) : <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No recent events</span>}
          </div>
        </div>
      </div>

      {/* Pose / Posture — rolling sample buffer rendered as a raw table.
          Each row is one minute's reading (newest first). See lelamp
          pose.py + motion.py: posture summary rides on the next
          motion.activity event only when the bad_ratio threshold is crossed. */}
      {pose ? (() => {
        const status = posePillStatus(pose);
        const win = pose.window_samples ?? 30;
        const samples = [...(pose.samples ?? [])].reverse(); // newest first
        const summary = pose.summary;
        return (
          <div style={S.card}>
            <CardHeader
              label="Pose / Posture"
              pill={<Pill text={status.text} color={status.color} />}
            />
            <div style={{ display: "grid", gridTemplateColumns: "minmax(240px, 320px) 1fr", gap: 14, marginBottom: 10, alignItems: "start" }}>
              <div>
                {/* Latest annotated pose snapshot. lelamp now keeps one JPEG
                    per sample under snapshots/<int(ts)>.jpg (rotated at 24h /
                    50MB), so the URL pins to the newest sample's ts. Table
                    rows below are clickable to view that specific frame. */}
                {(() => {
                  const newestTs = pose.samples?.[pose.samples.length - 1]?.ts;
                  const src = newestTs
                    ? `${HW}/sensing/pose-snapshot/${Math.floor(newestTs)}`
                    : `${HW}/sensing/pose-snapshot`;
                  return (
                    <img
                      src={src}
                      alt="latest pose"
                      style={{ width: "100%", borderRadius: 4, background: "var(--lm-text-muted)15", border: "1px solid var(--lm-text-muted)33" }}
                      onError={(e) => { (e.currentTarget.style.display = "none"); }}
                    />
                  );
                })()}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignContent: "flex-start" }}>
                <StatPill label="Buffer"      value={`${pose.samples_in_buffer ?? 0} / ${win}`} />
                <StatPill label="Until gate"  value={pose.samples_until_gate ?? win} color={(pose.samples_until_gate ?? 1) === 0 ? "var(--lm-green)" : undefined} />
                <StatPill label="Last"        value={fmtAgo(pose.seconds_since_sample)} />
                <StatPill label="Last score"  value={`${pose.ergo_score ?? "—"} (${riskName(pose.ergo_risk_level)})`} />
                {summary ? (
                  <>
                    <StatPill label="Bad" value={`${Math.round(summary.bad_ratio * 100)}% (${summary.bad_samples}/${summary.samples})`} color={summary.bad_ratio >= (pose.bad_ratio_threshold ?? 0.6) ? "var(--lm-red)" : "var(--lm-green)"} />
                    <StatPill label="Dominant" value={summary.dominant_region || "—"} />
                  </>
                ) : null}
              </div>
            </div>
            <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 4, letterSpacing: "0.05em", textTransform: "uppercase" }}>
              Samples (newest first) — sub-score@angle° raw from dlbackend; L = left, R = right
            </div>
            {samples.length === 0 ? (
              <span style={{ color: "var(--lm-text-muted)", fontSize: 11 }}>No samples yet</span>
            ) : (() => {
              // 10 columns: time, score, risk, neck, trunk, L u-arm, R u-arm, L l-arm, R l-arm, wrists
              const cols = "92px 42px 56px 90px 90px 90px 90px 90px 90px 64px";
              const fmtCell = (sub: number | undefined, angle: number | undefined): string => {
                if (sub == null) return "-";
                if (angle == null) return `${sub}`;
                return `${sub}@${Math.round(angle)}°`;
              };
              return (
                <div style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11, lineHeight: 1.6, overflowX: "auto" }}>
                  <div style={{ display: "grid", gridTemplateColumns: cols, gap: 6, color: "var(--lm-text-muted)", paddingBottom: 4, borderBottom: "1px solid var(--lm-text-muted)33", whiteSpace: "nowrap" }}>
                    <div>time</div><div>score</div><div>risk</div>
                    <div>neck</div><div>trunk</div>
                    <div>L u-arm</div><div>R u-arm</div>
                    <div>L l-arm</div><div>R l-arm</div>
                    <div>wrist L/R</div>
                  </div>
                  {samples.map((s, idx) => {
                    const d = new Date(s.ts * 1000);
                    const hh = String(d.getHours()).padStart(2, "0");
                    const mm = String(d.getMinutes()).padStart(2, "0");
                    const ss = String(d.getSeconds()).padStart(2, "0");
                    const L = s.left?.body_scores ?? {};
                    const R = s.right?.body_scores ?? {};
                    // Time cell links to that sample's annotated JPEG. lelamp
                    // keeps the file under snapshots/<int(ts)>.jpg until
                    // rotation prunes it (24h / 50MB caps), so older rows
                    // gracefully 404 once they age out.
                    const snapUrl = `${HW}/sensing/pose-snapshot/${Math.floor(s.ts)}`;
                    return (
                      <div key={`${s.ts}-${idx}`} style={{ display: "grid", gridTemplateColumns: cols, gap: 6, whiteSpace: "nowrap" }}>
                        <div>
                          <a
                            href={snapUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            title="View snapshot for this sample"
                            style={{ color: "var(--lm-blue, #4aa3ff)", textDecoration: "none" }}
                          >
                            {`${hh}:${mm}:${ss}`}
                          </a>
                        </div>
                        <div>{s.score}</div>
                        <div style={{ color: poseDotColor(s) }}>{riskName(s.risk_level)}</div>
                        <div>{fmtCell(L.neck, L.neck_angle)}</div>
                        <div>{fmtCell(L.trunk, L.trunk_angle)}</div>
                        <div>{fmtCell(L.upper_arm, L.upper_arm_angle)}</div>
                        <div>{fmtCell(R.upper_arm, R.upper_arm_angle)}</div>
                        <div>{fmtCell(L.lower_arm, L.lower_arm_angle)}</div>
                        <div>{fmtCell(R.lower_arm, R.lower_arm_angle)}</div>
                        <div>{`${L.wrist ?? "-"}/${R.wrist ?? "-"}`}</div>
                      </div>
                    );
                  })}
                </div>
              );
            })()}
          </div>
        );
      })() : null}
    </div>
  );
}
