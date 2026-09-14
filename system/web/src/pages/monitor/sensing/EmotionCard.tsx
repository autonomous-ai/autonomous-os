import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { Perception } from "./types";
import { fmtAgo } from "./format";

export function EmotionCard({ emotion }: { emotion: Perception | undefined }) {
  return (
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
  );
}
