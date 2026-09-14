import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { SensingData } from "./types";
import { fmtAgo, presenceColor } from "./format";

export function PresenceCard({ data }: { data: SensingData }) {
  return (
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
  );
}
