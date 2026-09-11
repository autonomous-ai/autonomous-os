import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { Perception } from "./types";
import { fmtAgo } from "./format";

export function MotionCard({ motion }: { motion: Perception | undefined }) {
  const motionFresh = (motion?.seconds_since_motion ?? Infinity) < 30;
  return (
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
  );
}
