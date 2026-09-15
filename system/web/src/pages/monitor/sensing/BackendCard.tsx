import { S } from "../styles";
import { StatPill } from "../components";
import { CardHeader } from "./CardHeader";
import type { SensingData } from "./types";

export function BackendCard({ data }: { data: SensingData }) {
  return (
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
  );
}
