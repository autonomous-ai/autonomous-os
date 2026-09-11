import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import { fmtAgo } from "./format";

export function EventsCard({ ev }: { ev: Record<string, number> }) {
  return (
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
  );
}
