import { useEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import { getSetupStatus } from "@/lib/api";
import { getInitialSearch } from "./useSetupUrlParams";

export type SetupPhase = "connecting" | "connected" | "failed";

// Three paired pollers driving the post-submit "Setting up…" UI:
//   (1) phase poll — runs while setupWorking, hits the AP IP for phase/lan_ip
//   (2) LAN probe — once we know the LAN IP, probe it from the browser; when
//       reachable (user rejoined home Wi-Fi) navigate there.
//   (3) mDNS probe — primary auto-redirect path. The LAN-IP channel almost
//       always fails in practice (AP shuts down before its lan_ip propagates
//       to the FE poll), so we also probe `lamp-XXXX.local` directly. When
//       the user's computer rejoins home Wi-Fi, mDNS resolves and we redirect.
export function useSetupStatusPolling({
  setupWorking,
  setupLanIP,
  deviceMdnsHost,
  setSetupPhase,
  setSetupLanIP,
  setSetupErrorMsg,
}: {
  setupWorking: boolean;
  setupLanIP: string;
  deviceMdnsHost: string;
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

  // Best-effort auto-redirect: once we know the LAN IP, probe it from the
  // browser. When reachable (= user has rejoined home Wi-Fi) navigate there.
  //
  // Gated on setupWorking (form submitted), NOT setupPhase==="connected": the
  // phase poll runs against the AP IP and usually goes dark when the AP shuts
  // down during the AP→STA switch, so it frequently never reports "connected".
  // A successful probe of the device's new LAN address is itself proof the
  // device is online and reachable — a stronger signal than the phase poll —
  // so we don't wait on the phase. We still only have setupLanIP once a phase
  // poll happened to return it before the AP died; the mDNS probe below is the
  // primary channel for the common case where it didn't.
  useEffect(() => {
    if (!setupWorking || !setupLanIP) return;
    let cancelled = false;
    const base = `http://${setupLanIP}`;
    // Carry pathname + original search across so the IP host lands back on
    // /setup with the OS-server-pushed params intact, same as the mDNS path.
    const newURL = `${base}${window.location.pathname}${carrySearch}`;
    const probe = async () => {
      try {
        // Raw-IP probe: works on every LAN regardless of mDNS, which is why
        // it's the reliable fallback when the router blocks multicast. The
        // device CSP must allow plain `http:` in connect-src for this fetch
        // to leave the browser (the IP is cross-origin from the AP page).
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (!cancelled) window.location.href = newURL;
      } catch {
        /* not reachable yet — user still on AP SSID */
      }
    };
    probe();
    const id = setInterval(probe, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [setupWorking, setupLanIP, carrySearch]);

  // mDNS probe — the primary auto-redirect channel since the LAN-IP one
  // rarely fires in real AP→STA transitions. Carries the current pathname +
  // search across, so any URL params from the OS server (llm_api_key, device_id, …)
  // remain in scope on the new host even though the OS server already persisted
  // them via the form submit. Manual button in Setup.tsx renders unconditionally
  // as the safety net if mDNS is blocked on the network.
  //
  // Gated on setupWorking (form submitted), NOT setupPhase==="connected". The
  // phase poll hits the AP IP and goes dark the moment the AP tears down for
  // the AP→STA switch, so it commonly never flips to "connected" — which used
  // to leave this probe disabled and the user stranded on the "connecting"
  // screen even after the device was fully online on home Wi-Fi (the exact
  // case the manual fallback link exists for). A successful health probe of
  // `<host>.local` is itself proof the device rejoined the LAN AND the user's
  // browser can resolve it, so it's the authoritative go-signal — we no longer
  // wait on the phase. The probe only succeeds once the user is back on home
  // Wi-Fi, so this can't fire prematurely while they're still on the AP.
  //
  // Critical: when the pre-submit redirect already moved us to the .local
  // URL, the target URL == current URL. Browsers no-op `location.href =
  // sameURL` — would leave the user stuck on the "connecting" screen even
  // though wifi is up. Force `reload()` for the same-host case so SetupGate
  // re-runs, hits the now-reachable `checkInternet`, and re-mounts Setup in
  // continue mode (full menu).
  useEffect(() => {
    if (!setupWorking || !deviceMdnsHost) return;
    let cancelled = false;
    const targetHost = `${deviceMdnsHost}.local`;
    const base = `http://${targetHost}`;
    const newURL = `${base}${window.location.pathname}${carrySearch}`;
    const navigate = () => {
      if (window.location.hostname === targetHost) {
        window.location.reload();
      } else {
        window.location.href = newURL;
      }
    };
    const probe = async () => {
      try {
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (!cancelled) navigate();
      } catch {
        /* mDNS not resolvable yet — user still on AP, or network blocks mDNS */
      }
    };
    probe();
    const id = setInterval(probe, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [setupWorking, deviceMdnsHost, carrySearch]);

  // Pre-submit canonical URL upgrade: when user lands on the AP IP
  // (192.168.100.1) we silently bounce to `http://lamp-XXXX.local/…` once we
  // know the hostname AND the .local name is reachable from the current
  // network. On the AP itself avahi runs on the device so the same multicast
  // reaches both peers — resolution is almost instant on Windows/macOS/iOS.
  // Benefit: the URL stays the same through the AP→STA wifi switch, so when
  // the user rejoins home Wi-Fi the browser reloads the same .local URL and
  // mDNS transparently maps it to the device's new LAN IP — no manual click.
  // Android Chrome (no native mDNS) just sees probes fail and stays on the
  // AP IP — current behavior, no regression.
  //
  // Aggressive timing on first attempts: browsers sometimes hold negative
  // mDNS results for a few seconds on the first lookup. Start polling at
  // 800ms and back off so the redirect lands sub-second when mDNS is healthy
  // without spamming the network forever on Android-blocked cases.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!deviceMdnsHost) {
      console.info("[setup] pre-submit canonical-URL upgrade: skip — no deviceMdnsHost yet");
      return;
    }
    if (window.location.hostname !== "192.168.100.1") {
      console.info("[setup] pre-submit canonical-URL upgrade: skip — not on AP IP", window.location.hostname);
      return;
    }
    console.info(`[setup] pre-submit canonical-URL upgrade: probing http://${deviceMdnsHost}.local/api/health`);
    let cancelled = false;
    let attempt = 0;
    const base = `http://${deviceMdnsHost}.local`;
    const target = `${base}${window.location.pathname}${carrySearch}`;
    let timer: number | undefined;
    const probe = async () => {
      attempt += 1;
      try {
        await fetch(`${base}/api/health`, { mode: "no-cors", cache: "no-store" });
        if (cancelled) return;
        console.info(`[setup] mDNS reachable after ${attempt} probe(s) — redirecting to ${target}`);
        window.location.replace(target);
        return;
      } catch (err) {
        console.info(`[setup] probe attempt ${attempt} failed`, err);
      }
      if (cancelled) return;
      // Back-off: 800ms × 4 then 2s × ∞ — fast initial retries when mDNS is
      // slow-resolving for the first lookup, then slow polls so we don't
      // hammer the network on Android-blocked clients.
      const next = attempt < 4 ? 800 : 2000;
      timer = window.setTimeout(probe, next);
    };
    probe();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [deviceMdnsHost, carrySearch]);
}
