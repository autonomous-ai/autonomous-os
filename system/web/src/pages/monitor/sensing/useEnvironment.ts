import { useState } from "react";
import { usePolling } from "../../../hooks/usePolling";
import { fetchEnvironmentStatus, type EnvironmentStatus } from "./environmentApi";

export function useEnvironment() {
  const [data, setData] = useState<EnvironmentStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  usePolling(async (signal) => {
    try {
      setData(await fetchEnvironmentStatus(signal));
      setError(null);
    } catch (cause) {
      setError(signal.aborted ? "Sensor request timed out." : cause instanceof Error ? cause.message : "Unable to reach sensor.");
    }
  }, 3000);

  return { data, error };
}
