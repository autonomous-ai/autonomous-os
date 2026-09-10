import { Link2 } from "lucide-react";
import { BuddyCard } from "./BuddyCard";
import { HarnessCard } from "./HarnessCard";

export function PairingSection() {
  return (
    <section>
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <span className="lm-mon-chip" aria-hidden><Link2 size={16} /></span>
          <h1 style={{ margin: 0, fontSize: 22, color: "var(--lm-text)" }}>Pairing</h1>
        </div>
        <p style={{ margin: 0, maxWidth: 620, fontSize: 13, lineHeight: 1.6, color: "var(--lm-text-dim)" }}>
          Connect a computer to this device with Autonomous Buddy or Harness.
        </p>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 16, alignItems: "start" }}>
        <BuddyCard />
        <HarnessCard />
      </div>
    </section>
  );
}
