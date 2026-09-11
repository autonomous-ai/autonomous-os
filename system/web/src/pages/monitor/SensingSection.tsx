import { EnvironmentCard } from "./sensing/EnvironmentCard";
import { VisionSensing } from "./sensing/VisionSensing";

export function SensingSection({ hasVision, hasEnvironment }: { hasVision: boolean; hasEnvironment: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {hasEnvironment && <EnvironmentCard />}
      {hasVision && <VisionSensing />}
    </div>
  );
}
