import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader, Pill } from "./CardHeader";
import type { Perception } from "./types";
import { fmtAgo, lightTier } from "./format";

export function LightCard({ light }: { light: Perception | undefined }) {
  return (
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
  );
}
