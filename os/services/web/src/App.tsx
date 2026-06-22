import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { SourceFooter } from "@/components/SourceFooter";
import Setup from "@/pages/Setup";
import Login from "@/pages/Login";
import Monitor from "@/pages/monitor";
import EditConfig from "@/pages/EditConfig";
import GwConfig from "@/pages/GwConfig";
import { checkInternet, getDeviceConfig, getSetupStatus, safeSearch, scrubLocationSecrets, setApiToken } from "@/lib/api";

// Detect Tailscale access by either:
//  - CGNAT IPv4 in 100.64.0.0/10 (100.64.0.0 – 100.127.255.255), or
//  - MagicDNS hostname (anything ending in `.ts.net`).
function isTailscaleHost(host: string): boolean {
  if (host.endsWith(".ts.net")) return true;
  const m = host.match(/^(\d+)\.(\d+)\./);
  if (!m) return false;
  const a = parseInt(m[1], 10);
  const b = parseInt(m[2], 10);
  return a === 100 && b >= 64 && b <= 127;
}

// Setup gate: provisioned (online) → continue mode (Voice/Face enroll, TTS
// preview), else initial mode (offline form for AP setup). When the user
// lands on the AP IP (192.168.4.1) but the device already has a real LAN IP
// (e.g. they bookmarked the AP URL after first setup), bounce them to the
// LAN address so the rest of the page works. `#force` in the URL hash
// forces initial mode for testing.
function SetupGate() {
  const force = typeof window !== "undefined" && window.location.hash === "#force";
  const [provisioned, setProvisioned] = useState<boolean | null>(force ? false : null);
  useEffect(() => {
    if (force) return;
    let cancelled = false;
    (async () => {
      const ok = await checkInternet().catch(() => false);
      if (cancelled) return;
      if (!ok) { setProvisioned(false); return; }
      // Online: see if we should redirect to the actual LAN IP first.
      // Skip redirect when the user is already reaching the device via its
      // Tailscale IP (CGNAT 100.64.0.0/10) — that's a deliberate remote-access
      // path, not the AP-IP-after-setup case we're trying to fix.
      try {
        const s = await getSetupStatus();
        if (cancelled) return;
        const here = window.location.hostname;
        // IP-only: we deliberately do NOT special-case the `.local` mDNS host.
        // The whole flow standardizes on the raw LAN IP because `.local`
        // silently fails on mDNS-blocking routers, so even when the browser
        // happens to be on `<type>-<id>.local` we still bounce to the IP — the
        // single reliable address. Only Tailscale (CGNAT) is skipped: that's a
        // deliberate remote-access path where the IP bounce doesn't apply.
        // safeSearch() keeps stripping secrets (llm_api_key, …) from the URL;
        // continue mode rehydrates them from saved config (has_llm_api_key),
        // so dropping them here is safe and avoids leaking secrets in the bar.
        if (s.lan_ip && s.lan_ip !== here && !isTailscaleHost(here)) {
          window.location.replace(`http://${s.lan_ip}${window.location.pathname}${safeSearch()}`);
          return;
        }
      } catch { /* keep showing continue mode if status endpoint fails */ }
      if (!cancelled) setProvisioned(true);
    })();
    return () => { cancelled = true; };
  }, [force]);
  if (provisioned === null) return null;
  return <Setup mode={provisioned ? "continue" : "initial"} />;
}

// AuthGate wraps protected routes. Hits GET /api/device/config to probe
// session state and route accordingly:
//
//   - 200 + has_admin_password=true  → render children (authed)
//   - 200 + has_admin_password=false → /setup (provisioned but admin not set —
//                                       migration window for devices upgrading
//                                       from pre-Login-UI builds)
//   - 401                            → /login (session missing/expired,
//                                       admin is configured)
//   - 503                            → /setup (admin auth not configured =
//                                       fresh device, never been set up)
//   - anything else                  → render children (network blip
//                                       shouldn't lock the user out; the next
//                                       admin call will surface the real
//                                       error)
function AuthGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const [state, setState] = useState<"checking" | "ok" | "login" | "setup">("checking");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getDeviceConfig();
        if (cancelled) return;
        if (!cfg.has_admin_password) {
          setState("setup");
        } else {
          setState("ok");
        }
      } catch (err) {
        if (cancelled) return;
        const status = (err as { status?: number })?.status;
        if (status === 401) setState("login");
        else if (status === 503) setState("setup");
        else setState("ok");
      }
    })();
    return () => { cancelled = true; };
  }, []);
  if (state === "checking") return null;
  if (state === "setup") {
    return <Navigate to={`/setup${safeSearch()}`} replace />;
  }
  if (state === "login") {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}

// RootRedirect lands operators on the right page for their auth state.
// Probes /api/device/config via AuthGate; on success it navigates to /monitor
// (the canonical "logged-in landing"). Unauthed → /login. Fresh → /setup.
// Lives at `/` so opening the root URL doesn't drop straight into the Setup
// form regardless of auth state.
function RootRedirect() {
  return (
    <AuthGate>
      <Navigate to="/monitor" replace />
    </AuthGate>
  );
}

// On every mount, scrub secret query params from the URL so they don't
// survive in browser history or address bar after the page reads them.
function useScrubSecrets() {
  useEffect(() => {
    scrubLocationSecrets();
  }, []);
}

// Pick up the bearer (llm_api_key) from the URL query and seed it into the
// Bearer header used by /api/* calls. Also exchanges it for a session cookie
// on the current origin so refresh / new tab keeps the user authed without
// needing the URL params again. Used by the post-setup AP→.local redirect:
// HTTP cookies are per-origin so the os_session set on 192.168.100.1
// doesn't carry to lamp-xxxx.local; useScrubSecrets() runs AFTER this
// to wipe the secret out of the address bar / browser history.
function useBearerFromQuery() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    const token = new URLSearchParams(window.location.search).get("llm_api_key");
    if (!token) return;
    setApiToken(token);
    // Issue session cookie on this origin. adminAuthMiddleware already
    // validates the Bearer the patched fetch attaches; the cookie's purpose
    // is to outlive the in-memory token for refresh / new-tab continuity.
    fetch("/api/login/exchange", { method: "POST" }).catch(() => {
      /* not fatal — Bearer still rides every /api/* call in this tab */
    });
  }, []);
}

function App() {
  // Order matters: pick up the URL bearer BEFORE useScrubSecrets() strips
  // `llm_api_key` from window.location. Both run as useEffects after the
  // first commit so declaration order = execution order.
  useBearerFromQuery();
  useScrubSecrets();
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<RootRedirect />} />
        <Route path="/setup" element={<SetupGate />} />
        <Route path="/login" element={<Login />} />
        {/* Monitor + Settings share ONE shell instance. Both paths are child
            routes of a single layout route whose element renders <Monitor/>.
            React Router keeps the layout element mounted while only the matched
            child path changes, so the sidebar does NOT remount when switching
            between /monitor and /setting (no full-page flash). Monitor derives
            its area ("monitor" | "setting") from useLocation().pathname. */}
        <Route element={<AuthGate><Monitor /></AuthGate>}>
          <Route path="/monitor" element={null} />
          <Route path="/setting" element={null} />
        </Route>
        <Route path="/edit" element={<AuthGate><EditConfig /></AuthGate>} />
        <Route path="/gw-config" element={<AuthGate><GwConfig /></AuthGate>} />
        <Route path="/dashboard" element={<Navigate to="/monitor" replace />} />
      </Routes>
      <Toaster richColors position="top-center" />
      <SourceFooter />
    </BrowserRouter>
  );
}

export default App;
