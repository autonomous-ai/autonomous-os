import { useEffect, useState } from "react";

// Device's DECLARED capabilities — the broad groups (audio, vision, motion, …)
// that os-server parses from devices/<type>/DEVICE.md and serves on
// /api/system/info. Go owns the contract; the web asks the OS rather than
// reaching through to the HAL runtime.
//
// Factored out of the Monitor page so other pages (e.g. Edit) gate
// hardware-specific UI the same way: a tab for a sensor/actuator the device
// lacks is hidden instead of dangling. null (not yet loaded, or the server
// declares none) → fail-open: show everything.
export function useCapabilities() {
  const [caps, setCaps] = useState<Set<string> | null>(null);
  useEffect(() => {
    fetch("/api/system/info")
      .then((r) => r.json())
      .then((r) => {
        if (r.status === 1 && r.data?.capabilities) {
          setCaps(new Set<string>(r.data.capabilities));
        }
      })
      .catch(() => {});
  }, []);
  // null caps (not yet loaded / none declared) → fail-open.
  const hasCap = (c: string): boolean => !caps || caps.has(c);
  return { caps, hasCap };
}
