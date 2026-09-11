import { MotionCard } from "./MotionCard";
import { EmotionCard } from "./EmotionCard";
import { FaceCard } from "./FaceCard";
import { PresenceCard } from "./PresenceCard";
import { LightCard } from "./LightCard";
import { SoundCard } from "./SoundCard";
import { BackendCard } from "./BackendCard";
import { EventsCard } from "./EventsCard";
import { PoseCard } from "./PoseCard";
import { useVisionSensing } from "./useVisionSensing";

export function VisionSensing() {
  const { data, error } = useVisionSensing();
  if (error) return <div role="alert" style={{ color: "var(--lm-amber)", padding: 20 }}>{error}</div>;
  if (!data) return <div style={{ color: "var(--lm-text-muted)", padding: 20 }}>Loading sensing data…</div>;
  const perception = (type: string) => data.perceptions.find((item) => item.type === type);
  const pose = perception("pose");
  return <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
    <div className="lm-grid-4">
      <MotionCard motion={perception("motion")} />
      <EmotionCard emotion={perception("emotion")} />
      <FaceCard face={perception("face")} />
      <PresenceCard data={data} />
    </div>
    <div className="lm-grid-4">
      <LightCard light={perception("light_level")} />
      <SoundCard sound={perception("sound")} />
      <BackendCard data={data} />
      <EventsCard ev={data.last_event_seconds_ago} />
    </div>
    {pose && <PoseCard pose={pose} />}
  </div>;
}
