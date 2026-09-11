import { useState } from "react";
import { usePolling } from "../../../hooks/usePolling";
import { getVisionSensing } from "./visionApi";
import type { SensingData } from "./types";

export function useVisionSensing() {
  const [data, setData] = useState<SensingData | null>(null);
  const [error, setError] = useState<string | null>(null);
  usePolling(async (signal) => {
    try {
      setData(await getVisionSensing(signal));
      setError(null);
    } catch (cause) {
      setError(signal.aborted ? "Sensing request timed out." : cause instanceof Error ? cause.message : "Unable to read sensing data.");
    }
  }, 3000);
  return { data, error };
}
