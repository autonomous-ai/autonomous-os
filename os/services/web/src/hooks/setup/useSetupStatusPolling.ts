import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getSetupStatus } from "@/lib/api";
import { getInitialSearch } from "./useSetupUrlParams";

export type SetupPhase = "connecting" | "connected" | "failed";

// The AP-mode static address (backend internal/device apSetupIP). Used to
// reject the AP's own IP when seeding lan_ip on a `.local` landing — we must
// never canonical-"upgrade" onto the soon-to-die AP address.
const AP_SETUP_IP = "192.168.100.1";

// How long the phase poll may go unanswered (while setupWorking) before we
// declare the AP dead. Normal cadence is 600ms, so 5s of silence means the
// AP tore down — not a slow response.
const AP_LOST_AFTER_MS = 5000;

// Pollers driving the post-submit "Setting up…" UI. Redirect is IP-FIRST —
// the raw LAN IP resolves on every network, so it's the primary target:
//   (1) phase poll — runs while setupWorking, hits the AP IP for phase/lan_ip
//       (the backend captures the STA IP early so the FE can read it during the
//       brief window the AP is still alive — see internal/device/service.go).
//   (2) LAN-IP probe — once lan_ip is known, probe http://<lan_ip>/api/health;
//       when it succeeds (operator rejoined home Wi-Fi, device is up) redirect
//       to http://<lan_ip>/setup. Also runs as the pre-submit canonical-URL
//       upgrade so the URL bar moves off the soon-to-die AP IP onto the IP that
//       survives the AP→STA switch. Requires the device CSP to allow plain
//       `http:` in connect-src (the IP is cross-origin from the AP page).
//   (3) mDNS `.local` fallback — DISCOVERY ONLY, fires only when the IP
//       channel is dead: on a first-time setup the AP often tears down before
//       wlan0 even has a DHCP lease, so the phase poll never reads lan_ip and
//       (2) has no target. Once the poll has been silent >5s (AP gone) we
//       probe http://<mdnsHost>.local/api/health and redirect there. `.local`
//       is NOT the durable home — many routers block mDNS multicast (and
//       Android Chrome has no native mDNS) — it's a best-effort bootstrap;
//       (4) immediately moves the page back onto the raw IP.
//   (4) `.local` landing seed — when the page is served from a `.local` host,
//       fetch lan_ip once from the open setup-status endpoint so probe (2)
//       canonical-upgrades the URL to the raw IP.
export function useSetupStatusPolling({
  setupWorking,
  setupLanIP,
  mdnsHost,
  setSetupPhase,
  setSetupLanIP,
  setSetupErrorMsg,
}: {
  setupWorking: boolean;
  setupLanIP: string;
  // Device hostname without the ".local" suffix (e.g. "lamp-a1b2"), derived
  // from the hardware MAC by Setup.tsx. Empty when the config hasn't loaded —
  // the mDNS fallback simply stays off in that case.
  mdnsHost: string;
  setSetupPhase: Dispatch<SetStateAction<SetupPhase>>;
  setSetupLanIP: Dispatch<SetStateAction<string>>;
  setSetupErrorMsg: Dispatch<SetStateAction<string>>;
}) {
  // Cross-origin redirect URL must carry every original param (incl.
  // llm_api_key) so the new host can rehydrate state + re-auth. Read via
  // the module-load snapshot — window.location.search at redirect time has
  // already been scrubbed by App.useScrubSecrets().
  const carrySearch = getInitialSearch();
  // Flips true when the phase poll has gone unanswered >5s while
  // setupWorking — the AP died before lan_ip could be read, so the IP-only
  // redirect can never fire and the mDNS fallback below takes over.
  const [apLost, setApLost] = useState(false);
  const lastPollOkRef = useRef(0);
  // Phase poll: runs against the AP IP, so it works while the user is still
  // on the AP SSID. Once the AP shuts down the polls will fail and we keep
  // the last value.
  useEffect(() => {
    if (!setupWorking) return;
    let cancelled = false;
    setApLost(false);
    lastPollOkRef.current = performance.now();
    const tick = async () => {
      try {
        const s = await getSetupStatus();
        if (cancelled) return;
        lastPollOkRef.current = performance.now();
        setApLost(false);
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
    // Poll fast (600ms). The AP at 192.168.100.1 only survives ~2s after the
    // AP→STA switch begins, and the backend publishes the captured lan_ip during
    // that window — so a slow 2s cadence can miss the one-or-two polls that land
    // while the AP is still answering, which is exactly what stranded the user.
    // Fast polling maximizes the chance we read lan_ip before the AP dies; once
    // it's dead the catch above just keeps the last phase and the LAN-IP probe
    // below takes over.
    tick();
    const id = setInterval(tick, 600);
    // Staleness watchdog on wall-clock, NOT on consecutive fetch failures —
    // fetches to a vanished AP can hang for many seconds in the browser's TCP
    // retry, which would delay AP-loss detection far past the real teardown.
    const watchdog = setInterval(() => {
      if (performance.now() - lastPollOkRef.current > AP_LOST_AFTER_MS) {
        setApLost(true);
      }
    }, 1000);
    return () => { cancelled = true; clearInterval(id); clearInterval(watchdog); };
  }, [setupWorking, setSetupPhase, setSetupLanIP, setSetupErrorMsg]);

  // IP-first auto-redirect. Once we know the device's LAN IP, probe it from the
  // browser; when the probe succeeds the user is back on home Wi-Fi and the
  // device is reachable, so navigate to http://<lan_ip>/setup?<params>.
  //
  // This is the PRIMARY redirect channel. A raw IP resolves on every LAN,
  // including the mDNS-blocked networks where the `.local` name silently
  // fails, so the IP is the preferred single target; `.local` (below) is only
  // a last-resort discovery path for when this channel never learned the IP.
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

  // mDNS `.local` fallback probe — rescues the first-time-setup race where the
  // AP tears down before wlan0 has a DHCP lease: the phase poll never reads
  // lan_ip, the operator's machine auto-rejoins home/office Wi-Fi, and the IP
  // probe above has no target — the page would be stranded on the dead AP IP
  // forever. `.local` is deliberately NOT a primary channel (routers that
  // block mDNS, Android Chrome), but when the IP channel is provably dead a
  // best-effort mDNS probe strictly improves the odds. Gates:
  //   - setupWorking: only post-submit; never yanks the pre-submit form away.
  //   - apLost:       only after the AP has been silent >5s. While the AP is
  //                   alive, mDNS may resolve OVER THE AP LINK (avahi answers
  //                   on all interfaces) and a premature redirect would reload
  //                   the page mid-join, losing the "Setting up…" state.
  //   - !setupLanIP:  the IP channel stays authoritative when it has a target.
  // On success we redirect to http://<host>.local/setup?<params>; the landing
  // seed below then bounces the page onto the raw IP.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!setupWorking || !apLost || setupLanIP || !mdnsHost) return;
    const host = `${mdnsHost}.local`;
    // Already on the .local host — same-origin polling works; nothing to do.
    if (window.location.hostname === host) return;
    let cancelled = false;
    const base = `http://${host}`;
    const target = `${base}${window.location.pathname}${carrySearch}`;
    let timer: number | undefined;
    let attempt = 0;
    const probe = async () => {
      attempt += 1;
      try {
        // Same CSP note as the IP probe: connect-src must allow plain `http:`.
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] device reachable at ${host} after ${attempt} mDNS probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch {
        /* mDNS blocked/unresolved, or operator not back on home Wi-Fi yet */
      }
      if (cancelled) return;
      // Flat 2s cadence — this only starts after the 5s AP-loss window, so
      // there's no sub-second race to win, and mDNS lookups are not cheap.
      timer = window.setTimeout(probe, 2000);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [setupWorking, apLost, setupLanIP, mdnsHost, carrySearch]);

  // `.local` landing seed: after the mDNS fallback redirect the page mounts
  // fresh on http://<host>.local with empty state — nothing would populate
  // lan_ip pre-submit, so the canonical-URL upgrade above could never move the
  // page onto the raw IP. Fetch it once from the open setup-status endpoint
  // (which falls back to the live wlan0 address when no setup run is in
  // flight — see internal/device/service.go SetupStatus). `.local` is only the
  // discovery bootstrap; the raw IP is the durable home, since mDNS can stop
  // resolving at any time on flaky networks. The AP_SETUP_IP guard prevents a
  // perverse "upgrade" onto the AP address if this ever runs in AP mode.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!window.location.hostname.endsWith(".local")) return;
    let cancelled = false;
    getSetupStatus().then((s) => {
      if (cancelled) return;
      if (s.lan_ip && s.lan_ip !== AP_SETUP_IP) {
        setSetupLanIP((prev) => prev || s.lan_ip);
      }
    }).catch(() => { /* status unreachable — stay on .local, page still works */ });
    return () => { cancelled = true; };
  }, [setSetupLanIP]);
}
