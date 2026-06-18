import { useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getSetupStatus } from "@/lib/api";
import { getInitialSearch } from "./useSetupUrlParams";

export type SetupPhase = "connecting" | "connected" | "failed";

// Two paired pollers driving the post-submit "Setting up…" UI. Redirect is
// IP-only by design — the device's `.local` mDNS name is deliberately NOT used
// because many home/office routers block mDNS multicast (and Android Chrome has
// no native mDNS), which leaves the operator stranded. A raw LAN IP resolves on
// every network, so it's the single source of truth for "where the device now
// lives":
//   (1) phase poll — runs while setupWorking, hits the AP IP for phase/lan_ip
//       (the backend captures the STA IP early so the FE can read it during the
//       brief window the AP is still alive — see internal/device/service.go).
//   (2) LAN-IP probe — once lan_ip is known, probe http://<lan_ip>/api/health;
//       when it succeeds (operator rejoined home Wi-Fi, device is up) redirect
//       to http://<lan_ip>/setup. Also runs as the pre-submit canonical-URL
//       upgrade so the URL bar moves off the soon-to-die AP IP onto the IP that
//       survives the AP→STA switch. Requires the device CSP to allow plain
//       `http:` in connect-src (the IP is cross-origin from the AP page).
export function useSetupStatusPolling({
  setupWorking,
  setupLanIP,
  setSetupPhase,
  setSetupLanIP,
  setSetupErrorMsg,
}: {
  setupWorking: boolean;
  setupLanIP: string;
  setSetupPhase: Dispatch<SetStateAction<SetupPhase>>;
  setSetupLanIP: Dispatch<SetStateAction<string>>;
  setSetupErrorMsg: Dispatch<SetStateAction<string>>;
}) {
  // Cross-origin redirect URL must carry every original param (incl.
  // llm_api_key) so the new host can rehydrate state + re-auth. Read via
  // the module-load snapshot — window.location.search at redirect time has
  // already been scrubbed by App.useScrubSecrets().
  const carrySearch = getInitialSearch();
  // Phase poll: runs against the AP IP, so it works while the user is still
  // on the AP SSID. Once the AP shuts down the polls will fail and we keep
  // the last value.
  useEffect(() => {
    if (!setupWorking) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await getSetupStatus();
        if (cancelled) return;
        if (s.phase === "connected") {
          setSetupPhase("connected");
          if (s.lan_ip) setSetupLanIP(s.lan_ip);
        } else if (s.phase === "failed") {
          setSetupPhase("failed");
          setSetupErrorMsg(s.error || "Wi-Fi setup failed.");
        }
      } catch {
        /* AP likely shutting down — keep last known phase */
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [setupWorking, setSetupPhase, setSetupLanIP, setSetupErrorMsg]);

  // IP-only auto-redirect. Once we know the device's LAN IP, probe it from the
  // browser; when the probe succeeds the user is back on home Wi-Fi and the
  // device is reachable, so navigate to http://<lan_ip>/setup?<params>.
  //
  // This is the ONLY redirect channel — there is no `.local` fallback. A raw
  // IP resolves on every LAN, including the mDNS-blocked networks where the
  // `.local` name silently fails, so the IP is the reliable single target.
  //
  // It also serves as the pre-submit canonical-URL upgrade: while the page is
  // on the AP IP (192.168.100.1) and a lan_ip is already known (e.g. re-setup
  // from a device that's still on home Wi-Fi, or after the early-capture poll
  // lands), it bounces the URL bar off the soon-to-die AP IP onto the IP that
  // survives the AP→STA switch. Before submit, with wlan0 still serving the AP
  // and no STA IP yet, lan_ip is empty and this effect simply does nothing —
  // the page stays on 192.168.100.1, exactly as intended.
  //
  // Not gated on setupWorking: it must also run pre-submit for the canonical
  // upgrade. It's safe — it can only fire once setupLanIP is non-empty, and
  // the probe only succeeds once the device is actually reachable at that IP.
  useEffect(() => {
    if (typeof window === "undefined" || !setupLanIP) return;
    // Already on the target IP — nothing to redirect to. (Avoids a same-URL
    // navigation no-op loop once we've landed.)
    if (window.location.hostname === setupLanIP) return;
    let cancelled = false;
    const base = `http://${setupLanIP}`;
    // Carry pathname + original search so the IP host lands back on /setup with
    // the OS-server-pushed params (llm_api_key, device_id, …) intact.
    const target = `${base}${window.location.pathname}${carrySearch}`;
    let attempt = 0;
    let timer: number | undefined;
    const probe = async () => {
      attempt += 1;
      try {
        // The device CSP must allow plain `http:` in connect-src for this
        // cross-origin fetch to leave the browser. `mode: "no-cors"` does not
        // bypass CSP — it only suppresses the opaque-response read.
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${setupLanIP} after ${attempt} probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* not reachable yet — user still on AP SSID, or device not up */
      }
      if (cancelled) return;
      // Back-off: 800ms × 4 then 2s × ∞ — fast initial retries so the redirect
      // lands sub-second when reachable, then slow polls so we don't hammer the
      // network while the user is still on the AP.
      const next = attempt < 4 ? 800 : 2000;
      timer = window.setTimeout(probe, next);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupLanIP, carrySearch]);
}
