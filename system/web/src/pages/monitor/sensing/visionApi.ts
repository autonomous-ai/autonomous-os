import { HW } from "../types";
import type { SensingData } from "./types";

export async function getVisionSensing(signal: AbortSignal): Promise<SensingData> {
  const response = await fetch(`${HW}/sensing`, { signal });
  if (!response.ok) throw new Error(`Unable to read sensing data (HTTP ${response.status}).`);
  const data: SensingData = await response.json();
  if (!Array.isArray(data.perceptions) || !data.presence || !data.last_event_seconds_ago) {
    throw new Error("Invalid sensing response.");
  }
  return data;
}

export function poseSnapshotUrl(timestamp: number): string {
  return `${HW}/sensing/pose-snapshot/${Math.floor(timestamp)}`;
}
