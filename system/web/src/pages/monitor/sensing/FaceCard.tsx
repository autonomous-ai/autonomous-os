import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { Perception } from "./types";
import { fmtAgo } from "./format";

export function FaceCard({ face }: { face: Perception | undefined }) {
  const faceVisible = (face?.visible?.length ?? 0) > 0;
  return (
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
  );
}
