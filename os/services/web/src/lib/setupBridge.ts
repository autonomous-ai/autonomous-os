// setupBridge — one-way event bridge from the device-local Setup page back to
// the window that opened it (e.g. autonomous.ai). The Setup page is served from
// the device's AP IP (http://192.168.100.1) or its .local host, a DIFFERENT
// origin from the site that opened the popup. The only cross-origin channel
// that works popup→opener (and iframe→parent) is window.postMessage, so this
// module wraps it in a single, safe `emit()` plus one named helper per event.
//
// ── How the parent (autonomous.ai) consumes these ──────────────────────────
// Open the popup carrying your origin so the device knows where to post:
//
//     const origin = encodeURIComponent(window.location.origin);
//     window.open(`http://192.168.100.1/setup?parent_origin=${origin}&...`, "_blank");
//
// Then listen — every message is a flat JSON object you can switch on:
//
//     window.addEventListener("message", (e) => {
//       // Accept the device's AP IP AND its <type>-<id>.local host (origin
//       // changes after the AP→home-WiFi handoff). Or just trust e.data.source.
//       if (e.data?.source !== "autonomous-device-setup") return;
//       switch (e.data.event) {
//         case "setup_opened":      /* popup is alive, wizard mounted */ break;
//         case "step_changed":      /* e.data.step === "wifi" | "llm" | ... */ break;
//         case "wifi_selected":     /* e.data.ssid */ break;
//         case "setup_submitted":   /* user clicked Setup — e.data.ssid/channel */ break;
//         case "setup_error":       /* validation/backend error — e.data.message */ break;
//         case "setup_connecting":  /* device is joining WiFi */ break;
//         case "setup_join_progress": /* still joining — e.data.elapsed_sec (e.g. 10) */ break;
//         case "setup_connected":   /* online — e.data.mdns_host / e.data.lan_ip */ break;
//         case "setup_failed":      /* join failed — e.data.message */ break;
//         case "retry_clicked":     /* user hit "Back to WiFi" after a failure */ break;
//         case "continue_clicked":  /* user clicked "Continue setup →" */ break;
//         case "monitor_clicked":   /* user clicked "Go to monitor →" */ break;
//       }
//     });
//
// Every payload is wrapped in this envelope:
//   { source: "autonomous-device-setup", v: 1, event: "<name>", ts: <ms>, ...data }
//
// `source` + `v` let the parent filter foreign messages and version the schema.
// emit() never throws and is a no-op when there's no opener/parent, so calling
// it from a normally-opened tab (not a popup) is harmless.

export const BRIDGE_SOURCE = "autonomous-device-setup" as const;
export const BRIDGE_VERSION = 1 as const;

// Closed set of events the Setup page can emit. Keep names stable — the parent
// switches on them. Add new ones here so callers stay type-checked.
export type SetupBridgeEvent =
  | "setup_opened"      // wizard mounted and ready
  | "step_changed"      // operator moved to another wizard step
  | "wifi_selected"     // a WiFi network was chosen
  | "setup_submitted"   // operator clicked Setup; request about to be sent
  | "setup_error"       // validation or backend error surfaced
  | "setup_connecting"  // device is joining WiFi (post-submit)
  | "setup_join_progress" // still joining after N seconds (heartbeat, e.g. 10s)
  | "setup_connected"   // device reached home WiFi and is reachable
  | "setup_failed"      // WiFi join failed
  | "retry_clicked"     // operator clicked "Back to Wi-Fi" after a failure
  | "continue_clicked"  // operator clicked "Continue setup →"
  | "monitor_clicked";  // operator clicked "Go to monitor →"

// Resolve the parent origin ONCE at module load. Priority:
//   1) ?parent_origin=… passed by whoever opened the popup (explicit, safest).
//   2) document.referrer's origin (the opener URL, when the param is omitted).
//   3) "*" as a last resort so events still flow if neither is available.
// Using a concrete origin is preferred — "*" lets any window read the payload,
// but we never put secrets in these events, so it's an acceptable fallback.
function resolveParentOrigin(): string {
  if (typeof window === "undefined") return "*";
  try {
    const fromParam = new URLSearchParams(window.location.search).get("parent_origin");
    if (fromParam) return new URL(fromParam).origin;
  } catch {
    /* malformed parent_origin — fall through */
  }
  try {
    if (document.referrer) return new URL(document.referrer).origin;
  } catch {
    /* no/blocked referrer — fall through */
  }
  return "*";
}

const PARENT_ORIGIN = resolveParentOrigin();

// Targets we post to. A popup talks to window.opener; an embedded iframe talks
// to window.parent. We try both (skipping self) so the bridge works either way.
function targets(): Window[] {
  if (typeof window === "undefined") return [];
  const out: Window[] = [];
  if (window.opener && window.opener !== window) out.push(window.opener as Window);
  if (window.parent && window.parent !== window) out.push(window.parent);
  return out;
}

// emit — post one event to the parent/opener. Safe to call unconditionally:
// no targets → no-op; postMessage failures are swallowed so the Setup flow is
// never affected by a messaging error.
export function emit(event: SetupBridgeEvent, data: Record<string, unknown> = {}): void {
  const payload = {
    source: BRIDGE_SOURCE,
    v: BRIDGE_VERSION,
    event,
    ts: Date.now(),
    ...data,
  };
  const dests = targets();
  // Log every event so it's visible in the device page's console even when no
  // opener is listening — handy for debugging the setup flow on the device.
  console.log(`[setupBridge] ${event} → ${dests.length} target(s) @ ${PARENT_ORIGIN}`, payload);
  for (const target of dests) {
    try {
      target.postMessage(payload, PARENT_ORIGIN);
    } catch {
      /* cross-origin / closed window — ignore, this channel is best-effort */
    }
  }
}

// Named helpers — one per event. Thin wrappers over emit() so call sites read
// declaratively and the payload shape for each event lives in one place.
export const setupBridge = {
  opened: (info: { mode: string; deviceId?: string; mac?: string }) =>
    emit("setup_opened", info),
  stepChanged: (step: string) => emit("step_changed", { step }),
  wifiSelected: (ssid: string) => emit("wifi_selected", { ssid }),
  submitted: (info: { ssid: string; channel: string }) =>
    emit("setup_submitted", info),
  error: (message: string) => emit("setup_error", { message }),
  connecting: () => emit("setup_connecting", {}),
  // Heartbeat fired once the device has been joining WiFi for `elapsedSec`
  // seconds (e.g. 10s) without yet flipping to connected/failed. Lets the parent
  // window know the join is taking a moment but is still in progress — useful
  // for showing its own "still working…" hint or analytics on slow joins.
  joinProgress: (elapsedSec: number) =>
    emit("setup_join_progress", { elapsed_sec: elapsedSec }),
  connected: (info: { mdns_host?: string; lan_ip?: string }) =>
    emit("setup_connected", info),
  failed: (message: string) => emit("setup_failed", { message }),
  retryClicked: () => emit("retry_clicked", {}),
  continueClicked: (info: { mdns_host?: string }) =>
    emit("continue_clicked", info),
  monitorClicked: () => emit("monitor_clicked", {}),
};
