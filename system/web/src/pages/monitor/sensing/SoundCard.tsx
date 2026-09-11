import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { Perception } from "./types";

export function SoundCard({ sound }: { sound: Perception | undefined }) {
  return (
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
  );
}
