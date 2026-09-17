import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Music, Eye, EyeOff, Lock, ExternalLink, AlertTriangle,
  ChevronDown, ChevronRight,
} from "lucide-react";
import { C } from "@/components/setup/shared";
import { getConnector, setConnectorPAT, removeConnector, exchangeSpotifyCode } from "@/lib/api";
import type { ConnectorInfo } from "@/lib/api";

// SpotifySection — the on-device settings surface for the Spotify Web API +
// Connect playback skill. Layout mirrors the ecm-website admin PAT connector
// modal (SPOTIFY_PAT in connectorAuthRegistry) and FacebookSection's shape,
// with three inputs instead of two because Spotify's OAuth flow needs the
// Client ID / Client Secret alongside the durable refresh_token.
//
// Storage: NOT config.json. This writes through the same connectorWriter the
// MQTT connector.set.<code> dispatcher uses, via POST
// /api/device/connectors/pat → <OpenclawConfigDir>/workspace/configs/
// spotify_access_tokens.json. That is the exact file the music skill reads
// and the same file the ecm-website admin's connector flow would write when
// it ships — no drift between local paste and remote push.
//
// Field-to-BE mapping (matches ecm-website SPOTIFY_PAT):
//   api_key                 ← the pasted refresh_token (durable credential)
//   credentials.client_id   ← Spotify Developer app Client ID (non-secret)
//   credentials.client_sec  ← Spotify Developer app Client Secret (secret)
// The device's refresh loop will trade (refresh_token + client_id + secret)
// for a short-lived access_token whenever it needs to call api.spotify.com.
//
// This section renders its own Cancel/Connect footer (SectionCard-scoped, not
// modal-shaped) so SettingsPanel excludes "spotify" from the shared Save
// button chrome. Cancel resets the local form; Connect submits.
const CONNECTOR_CODE = "spotify";

// Scopes we ask for during the Authorization Code flow. `streaming` is
// non-negotiable — without it, playback commands 403 with PREMIUM_REQUIRED
// even on a Premium account, because the mint didn't grant playback rights.
const SPOTIFY_SCOPES = [
  "streaming",
  "user-modify-playback-state",
  "user-read-playback-state",
  "user-read-currently-playing",
  "playlist-read-private",
  "user-library-read",
].join(" ");
const SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback";

// Compose the fully-formed Spotify authorize URL with the user's Client ID
// pre-substituted so the guide can render a one-click button instead of a
// paste-then-edit URL. Scopes + redirect URI are URL-encoded per RFC 6749.
function buildSpotifyAuthorizeURL(clientId: string): string {
  const params = new URLSearchParams({
    client_id: clientId,
    response_type: "code",
    redirect_uri: SPOTIFY_REDIRECT_URI,
    scope: SPOTIFY_SCOPES,
  });
  return `https://accounts.spotify.com/authorize?${params.toString()}`;
}

// Compose the token-exchange curl with Client ID + Secret + Redirect URI
// pre-substituted. CODE stays as a placeholder — the user pastes their own
// code from step b. Newline continuations are used so the block reads like
// a real terminal command instead of one massive line.
function buildSpotifyTokenCurl(clientId: string, clientSecret: string): string {
  return `curl -s -X POST https://accounts.spotify.com/api/token \\
  -u "${clientId}:${clientSecret}" \\
  -d grant_type=authorization_code \\
  -d code=CODE \\
  -d redirect_uri=${SPOTIFY_REDIRECT_URI}`;
}

export function SpotifySection({ active }: { active: boolean }) {
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [connectedClientId, setConnectedClientId] = useState<string>("");
  const [connectedAt, setConnectedAt] = useState<number>(0);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [showToken, setShowToken] = useState(false);
  // Guide starts collapsed to keep the form short. Persist the choice per-
  // browser so an operator who wants it open doesn't have to re-expand on
  // every visit; fall back to false on any storage error (private windows).
  const [guideOpen, setGuideOpen] = useState<boolean>(() => {
    try { return localStorage.getItem("spotify-guide-open") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("spotify-guide-open", guideOpen ? "1" : "0"); } catch { /* private mode: silently skip */ }
  }, [guideOpen]);
  const [submitting, setSubmitting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Step 4c "send code to device" state — the code the user pasted from the
  // OAuth callback URL bar, plus the in-flight / result signals for the
  // exchange call. Kept local to the guide (not part of the main form) so
  // clearing the form does not blow away a pasted code the user is about to
  // exchange.
  const [oauthCode, setOauthCode] = useState("");
  const [exchanging, setExchanging] = useState(false);
  const [exchangeError, setExchangeError] = useState<string | null>(null);

  const canExchange =
    clientId.trim() !== "" && clientSecret.trim() !== "" && oauthCode.trim() !== "";

  const onExchangeCode = async () => {
    if (!canExchange || exchanging) return;
    setExchangeError(null);
    setExchanging(true);
    try {
      const res = await exchangeSpotifyCode({
        code: oauthCode.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
      });
      // Fill the refresh token field so the operator sees what got saved,
      // then flip the section into "connected" state by re-reading the file
      // from the server — same code path as a successful Connect click. The
      // credential is already persisted at this point (the endpoint stored
      // it via connectorWriter server-side).
      setRefreshToken(res.refresh_token);
      setOauthCode("");
      toast.success("Spotify connected — refresh token saved.");
      load();
    } catch (err) {
      setExchangeError(err instanceof Error ? err.message : "Exchange failed.");
    } finally {
      setExchanging(false);
    }
  };

  const load = () => {
    setLoading(true);
    getConnector(CONNECTOR_CODE)
      .then((r: ConnectorInfo) => {
        setConnected(!!r.connected);
        // Only surface the Client ID as connected-state metadata — the
        // refresh_token and secret are never shown back to the operator.
        setConnectedClientId(r.credentials?.client_id ?? "");
        setConnectedAt(r.obtained_at ?? 0);
      })
      .catch(() => {
        // A device that has never had this connector set answers with
        // connected:false; a hard error only fires on 500. Silent on 500 is
        // fine — the UI shows the empty form and the operator can try again.
      })
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const reset = () => {
    setClientId("");
    setClientSecret("");
    setRefreshToken("");
    setError(null);
    setShowSecret(false);
    setShowToken(false);
  };

  // Explicit click handler, not a form submit — SpotifySection is rendered
  // inside SettingsPanel's shared <form id="edit-form"> and HTML forbids
  // nested forms. See the same note in FacebookSection.
  const onSubmit = async (e: React.MouseEvent | React.KeyboardEvent) => {
    e.preventDefault();
    setError(null);
    const id = clientId.trim();
    const secret = clientSecret.trim();
    const token = refreshToken.trim();
    if (!id) { setError("Client ID is required."); return; }
    // Spotify Client IDs are 32 hex chars today, but the length is not
    // formally documented as fixed. Guard on a loose range and character
    // class instead of an exact match.
    if (!/^[a-f0-9]{20,64}$/i.test(id)) {
      setError("Client ID should be a long hexadecimal string from your Spotify Developer app.");
      return;
    }
    if (!secret) { setError("Client Secret is required."); return; }
    if (secret.length < 20) {
      setError("Client Secret looks too short — copy the whole value from the app's Settings page.");
      return;
    }
    if (!token) { setError("Refresh Token is required."); return; }
    if (token.length < 40) {
      setError("Refresh Token looks too short — copy the whole `refresh_token` value from the OAuth callback.");
      return;
    }
    setSubmitting(true);
    try {
      await setConnectorPAT({
        connector: CONNECTOR_CODE,
        api_key: token,
        credentials: { client_id: id, client_secret: secret },
      });
      toast.success("Spotify connected.");
      reset();
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const onDisconnect = async () => {
    if (!connected) return;
    if (!window.confirm("Disconnect Spotify? The device will stop being able to play music until you reconnect.")) return;
    setDisconnecting(true);
    try {
      await removeConnector(CONNECTOR_CODE);
      toast.success("Spotify disconnected.");
      setConnected(false);
      setConnectedClientId("");
      setConnectedAt(0);
      reset();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Disconnect failed.");
    } finally {
      setDisconnecting(false);
    }
  };

  // Bypass SectionCard: this section renders a modal-style card whose own
  // header replaces SectionCard's title chip, so we mount/unmount inline and
  // keep the display:none idiom the other sections use.
  return (
    <div
      id="section-spotify"
      className="lm-card lm-fade-in"
      style={{
        display: active ? "block" : "none",
        padding: "22px 24px",
        marginBottom: 16,
      }}
    >
      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <div
          onKeyDown={(e) => {
            // Preserve the Enter-to-submit affordance without a real <form>.
            // Only fires when one of the credential inputs is focused; keeps
            // the behaviour narrow so a future textarea won't submit.
            const target = e.target as HTMLElement;
            if (e.key === "Enter" && target.tagName === "INPUT") {
              if (!submitting && clientId && clientSecret && refreshToken) onSubmit(e);
            }
          }}
        >
          <ModalHeader
            connected={connected}
            connectedAt={connectedAt}
            connectedClientId={connectedClientId}
          />

          <InfoCard tone="blue" title="BEFORE YOU START">
            You need a <b>Spotify Premium</b> account. Free accounts can&apos;t
            play music through third-party clients — Spotify gates the
            streaming API to Premium.
          </InfoCard>

          <FieldLabel htmlFor="spotify_client_id">Client ID</FieldLabel>
          <input
            id="spotify_client_id"
            type="text"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            placeholder="e.g. 8a1b2c3d4e5f6789abcdef0123456789"
            autoComplete="off"
            name="spotify-client-id-nolockup"
            data-lpignore="true"
            data-1p-ignore="true"
            data-form-type="other"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 6, fontFamily: "monospace" }}
          />
          {/* Guide-link hint under the first field — Facebook uses the same
              pattern to keep how-to copy in ONE place (the guide) instead of
              duplicating it inline. openGuide → expand + scroll into view. */}
          <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 14, lineHeight: 1.5 }}>
            Don&apos;t have a Spotify app yet?{" "}
            <button
              type="button"
              onClick={() => {
                setGuideOpen(true);
                document
                  .getElementById("spotify-guide-anchor")
                  ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
              }}
              style={{
                background: "none", border: "none", padding: 0,
                color: C.amber, cursor: "pointer",
                font: "inherit", fontWeight: 500, textDecoration: "underline",
                textUnderlineOffset: 2,
              }}
            >
              See how to create one
            </button>
            .
          </div>

          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 5 }}>
            <FieldLabel htmlFor="spotify_client_secret">Client Secret</FieldLabel>
            <div style={{ fontSize: 11, color: C.textMuted, display: "flex", alignItems: "center", gap: 8 }}>
              <button
                type="button"
                onClick={() => setShowSecret((v) => !v)}
                aria-label={showSecret ? "Hide" : "Show"}
                style={{ background: "none", border: "none", padding: 0, color: C.textMuted, cursor: "pointer", display: "flex", alignItems: "center" }}
              >
                {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </div>
          <input
            id="spotify_client_secret"
            type={showSecret ? "text" : "password"}
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder="paste your Client Secret"
            autoComplete="new-password"
            name="spotify-client-secret-nolockup"
            data-lpignore="true"
            data-1p-ignore="true"
            data-form-type="other"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 12, fontFamily: "monospace" }}
          />

          <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 5 }}>
            <FieldLabel htmlFor="spotify_refresh_token">Refresh Token</FieldLabel>
            <div style={{ fontSize: 11, color: C.textMuted, display: "flex", alignItems: "center", gap: 8 }}>
              <TokenCounter length={refreshToken.length} />
              <button
                type="button"
                onClick={() => setShowToken((v) => !v)}
                aria-label={showToken ? "Hide" : "Show"}
                style={{ background: "none", border: "none", padding: 0, color: C.textMuted, cursor: "pointer", display: "flex", alignItems: "center" }}
              >
                {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </div>
          <input
            id="spotify_refresh_token"
            type={showToken ? "text" : "password"}
            value={refreshToken}
            onChange={(e) => setRefreshToken(e.target.value)}
            placeholder="paste the refresh_token from the OAuth callback"
            autoComplete="new-password"
            name="spotify-refresh-token-nolockup"
            data-lpignore="true"
            data-1p-ignore="true"
            data-form-type="other"
            spellCheck={false}
            style={{ ...inputStyle, marginBottom: 12, fontFamily: "monospace" }}
          />

          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            fontSize: 11, color: C.amber, marginBottom: 16,
          }}>
            <Lock size={11} /> Stored on your device only. Never uploaded to our servers.
          </div>

          {/* Collapsible walkthrough — placed BELOW the form fields so an
              operator with credentials in hand sees the form first without
              scrolling past a wall of steps. Header row is always visible so
              people who need the walkthrough know where to click. Expanded
              state persisted in localStorage — see guideOpen. The `id` is a
              scroll target for the Client-ID hint link above so a click on
              "See how to create one" jumps here. */}
          <div id="spotify-guide-anchor" style={{
            background: C.bg, border: `1px solid ${C.border}`, borderRadius: 10,
            padding: "10px 14px", marginBottom: 12,
          }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <button
                type="button"
                onClick={() => setGuideOpen((v) => !v)}
                style={{
                  background: "none", border: "none", padding: 0, cursor: "pointer",
                  display: "inline-flex", alignItems: "center", gap: 6,
                  color: C.textDim, fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em",
                }}
                aria-expanded={guideOpen}
              >
                {guideOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                HOW TO GET YOUR SPOTIFY CREDENTIALS
              </button>
              <a
                href="https://developer.spotify.com/dashboard"
                target="_blank"
                rel="noreferrer"
                style={{ color: C.amber, fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 4, textDecoration: "none" }}
              >
                Open Spotify Dashboard <ExternalLink size={11} />
              </a>
            </div>
            {guideOpen && (
              <div style={{ marginTop: 12, fontSize: 11.5, color: C.textDim, lineHeight: 1.6 }}>
                <Step n={1}>
                  Open the <b>Spotify Developer Dashboard</b> and sign in with
                  your Premium account. Click <b>Create app</b>.
                </Step>
                <Step n={2}>
                  Fill in any name and description. In <b>Redirect URIs</b> add{" "}
                  <span style={{
                    background: C.surface, border: `1px solid ${C.border}`, borderRadius: 4,
                    padding: "1px 5px", fontFamily: "monospace", fontSize: 10.5, color: C.text,
                  }}>http://127.0.0.1:8888/callback</span>{" "}
                  (exactly this — the token helper uses the same one). Under{" "}
                  <b>Which API/SDKs are you planning to use?</b>{" "}
                  tick <b>Web API</b> ONLY — leave Web Playback SDK, Android,
                  iOS and Ads API unchecked (those are for browser JS / mobile
                  apps, not this device). Save.
                </Step>
                <Step n={3}>
                  Open the app you just made → <b>Settings</b>. Copy the{" "}
                  <b>Client ID</b> and click <b>View client secret</b> to copy
                  the <b>Client Secret</b>. Paste both above.
                </Step>
                <Step n={4}>
                  <b style={{ color: C.amber }}>Mint a Refresh Token</b> — no
                  server needed, just a browser + one curl:
                  <SubStep letter="a">
                    {clientId.trim() ? (
                      <>
                        Click this link to open Spotify&apos;s authorize page
                        (your Client ID is baked in):
                        <div style={{ margin: "6px 0 0" }}>
                          <a
                            href={buildSpotifyAuthorizeURL(clientId.trim())}
                            target="_blank"
                            rel="noreferrer"
                            style={{
                              display: "inline-flex", alignItems: "center", gap: 6,
                              padding: "8px 14px", borderRadius: 8,
                              background: "#1DB954", color: "#fff",
                              fontSize: 12, fontWeight: 600, textDecoration: "none",
                            }}
                          >
                            <Music size={13} /> Authorize with Spotify
                            <ExternalLink size={11} />
                          </a>
                        </div>
                      </>
                    ) : (
                      <>
                        <b style={{ color: C.amber }}>Paste your Client ID above first</b>
                        {" "}— then a one-click <i>Authorize with Spotify</i>
                        {" "}button will appear here with your ID baked in.
                      </>
                    )}
                  </SubStep>
                  <SubStep letter="b">
                    Sign in with your Premium account → click{" "}
                    <b>Agree</b>. The browser will try to open{" "}
                    <i>127.0.0.1:8888</i> and show{" "}
                    <i>&quot;This site can&apos;t be reached&quot;</i> — that&apos;s fine. Look
                    at the URL bar: it now ends with{" "}
                    <Kbd>?code=AQD...</Kbd>. Copy the long string after{" "}
                    <Kbd>code=</Kbd> (before <Kbd>&amp;</Kbd> if any).
                  </SubStep>
                  <SubStep letter="c" last>
                    {clientId.trim() && clientSecret.trim() ? (
                      <>
                        Paste the code from step b below and hit{" "}
                        <b>Send to device</b>. The device does the token
                        exchange with Spotify and saves the refresh token —
                        no terminal needed.
                        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                          <input
                            type="text"
                            value={oauthCode}
                            onChange={(e) => setOauthCode(e.target.value)}
                            placeholder="paste the ?code=... value here"
                            autoComplete="off"
                            spellCheck={false}
                            style={{ ...inputStyle, fontFamily: "monospace", flex: 1 }}
                          />
                          <button
                            type="button"
                            onClick={onExchangeCode}
                            disabled={!canExchange || exchanging}
                            style={{
                              padding: "8px 14px", borderRadius: 8,
                              background: !canExchange || exchanging ? C.surface : "#1DB954",
                              color: !canExchange || exchanging ? C.textMuted : "#fff",
                              border: "none",
                              fontSize: 11.5, fontWeight: 600,
                              cursor: !canExchange || exchanging ? "not-allowed" : "pointer",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {exchanging ? "Sending…" : "Send to device"}
                          </button>
                        </div>
                        {exchangeError && (
                          <div style={{
                            marginTop: 6, padding: "6px 8px", borderRadius: 6,
                            background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
                            color: C.red, fontSize: 10.5,
                          }}>
                            {exchangeError}
                          </div>
                        )}
                        <details style={{ marginTop: 8 }}>
                          <summary style={{ cursor: "pointer", fontSize: 10.5, color: C.textMuted }}>
                            Prefer to run curl yourself?
                          </summary>
                          <CodeBlockWithCopy
                            text={buildSpotifyTokenCurl(clientId.trim(), clientSecret.trim())}
                          />
                        </details>
                      </>
                    ) : (
                      <>
                        Paste your <b>Client Secret</b> above too — then this
                        step will show a &quot;Send code to device&quot;
                        button that does the token exchange for you (no
                        terminal needed).
                      </>
                    )}
                  </SubStep>
                  <div style={{ marginTop: 8, color: C.textMuted, fontSize: 10.5 }}>
                    <b>streaming</b> scope is required for the device to play
                    audio. Without it, playback commands succeed but no music
                    plays.
                  </div>
                </Step>
                <Step n={5} last>
                  Paste the Refresh Token above, then click <b>Connect</b>. The
                  device swaps it for a fresh access_token every hour and{" "}
                  <b>rotates the refresh_token itself</b> whenever Spotify
                  returns a new one — so as long as music plays at least once
                  every 6 months, this keeps working.
                </Step>
              </div>
            )}
          </div>

          <InfoCard tone="warning" title={null} icon={<AlertTriangle size={14} color={C.amber} />}>
            Spotify refresh tokens now expire after <b>180 days</b> (shown on
            the app dashboard as <i>Refresh Token Lifetime · 180 days</i>).
            The device auto-rotates on every hourly refresh, so an active
            device never hits it. It only expires if the device sits unused
            past 180 days or you revoke the app at{" "}
            <a
              href="https://www.spotify.com/account/apps/"
              target="_blank"
              rel="noreferrer"
              style={{ color: C.amber }}
            >
              spotify.com/account/apps
            </a>
            .
          </InfoCard>

          {error && (
            <div style={{
              background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
              borderRadius: 8, padding: "8px 12px", fontSize: 11.5, color: C.red, marginBottom: 14,
            }}>
              {error}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 }}>
            {connected ? (
              <button
                type="button"
                onClick={onDisconnect}
                disabled={disconnecting}
                style={{
                  padding: "8px 14px", borderRadius: 8, fontSize: 12, fontWeight: 500,
                  background: "none",
                  border: `1px solid ${C.border}`,
                  color: C.red,
                  cursor: disconnecting ? "not-allowed" : "pointer",
                  opacity: disconnecting ? 0.5 : 1,
                }}
              >
                {disconnecting ? "Disconnecting…" : "Disconnect"}
              </button>
            ) : <span />}

            <div style={{ display: "flex", gap: 10 }}>
              <button
                type="button"
                onClick={reset}
                disabled={submitting || (!clientId && !clientSecret && !refreshToken)}
                style={{
                  padding: "8px 18px", borderRadius: 8, fontSize: 12, fontWeight: 500,
                  background: "none",
                  border: `1px solid ${C.border}`,
                  color: C.text,
                  cursor: submitting || (!clientId && !clientSecret && !refreshToken) ? "not-allowed" : "pointer",
                  opacity: submitting || (!clientId && !clientSecret && !refreshToken) ? 0.5 : 1,
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onSubmit}
                disabled={submitting || !clientId || !clientSecret || !refreshToken}
                style={{
                  padding: "8px 22px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                  background: submitting || !clientId || !clientSecret || !refreshToken ? C.surface : C.amber,
                  color: submitting || !clientId || !clientSecret || !refreshToken ? C.textMuted : "var(--lm-on-amber)",
                  border: "none",
                  cursor: submitting || !clientId || !clientSecret || !refreshToken ? "not-allowed" : "pointer",
                  transition: "all 0.15s",
                }}
              >
                {submitting ? "Connecting…" : connected ? "Reconnect" : "Connect"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

// ModalHeader — logo + title + description row. Named "Modal" because it
// visually anchors the card like the modal header of the ecm-website's PAT
// connector, even though this renders in-page (not a real dialog).
function ModalHeader({
  connected,
  connectedAt,
  connectedClientId,
}: {
  connected: boolean;
  connectedAt: number;
  connectedClientId: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 14, marginBottom: 18 }}>
      <div style={{
        flexShrink: 0,
        width: 44, height: 44, borderRadius: 12,
        background: "#1DB954",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        <Music size={24} color="#fff" strokeWidth={2.4} />
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: C.text, marginBottom: 3 }}>
          Connect Spotify
        </div>
        <div style={{ fontSize: 12, color: C.textDim, lineHeight: 1.5 }}>
          Add your Spotify Developer credentials so this device can play music
          through your Premium account.
        </div>
        {connected && (
          <div style={{ marginTop: 8, fontSize: 11, color: C.green, display: "inline-flex", alignItems: "center", gap: 6 }}>
            ✓ connected
            {connectedClientId && (
              <span style={{ color: C.textMuted }}>
                · app {connectedClientId.slice(0, 8)}…
              </span>
            )}
            {connectedAt > 0 && (
              <span style={{ color: C.textMuted }}>
                · {new Date(connectedAt * 1000).toLocaleDateString()}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function InfoCard({
  tone,
  title,
  titleRight,
  icon,
  children,
}: {
  tone: "blue" | "warning" | "plain";
  title: string | null;
  titleRight?: React.ReactNode;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const bg = tone === "warning" ? "var(--lm-amber-dim)" : tone === "blue" ? "var(--lm-info-bg, rgba(30,64,175,0.10))" : C.bg;
  const border = tone === "warning" ? "var(--lm-amber-glow)" : tone === "blue" ? "var(--lm-info-border, rgba(59,130,246,0.35))" : C.border;
  const titleColor = tone === "warning" ? C.amber : tone === "blue" ? C.amber : C.textDim;
  return (
    <div style={{
      background: bg, border: `1px solid ${border}`, borderRadius: 10,
      padding: "12px 14px", marginBottom: 14, fontSize: 11.5, color: C.textDim, lineHeight: 1.6,
    }}>
      {(title || titleRight || icon) && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: title ? 8 : 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {icon}
            {title && <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.05em", color: titleColor }}>{title}</div>}
          </div>
          {titleRight}
        </div>
      )}
      <div>{children}</div>
    </div>
  );
}

// Nested a/b/c step inside a numbered <Step>. Used when a single top-level
// step needs its own ordered sub-list — e.g. Spotify's "mint a refresh_token"
// walk-through has 4 discrete actions (open URL / read URL bar / run curl /
// paste value). Lowercase letter + tighter spacing so it reads as an aside.
function SubStep({ letter, last, children }: { letter: string; last?: boolean; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 8, marginTop: 6, marginBottom: last ? 0 : 0 }}>
      <div style={{
        flexShrink: 0, width: 14, color: C.amber, fontSize: 10.5, fontWeight: 700, marginTop: 2,
      }}>{letter}.</div>
      <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.6, minWidth: 0 }}>{children}</div>
    </div>
  );
}

// Inline monospace token (URL fragment, JSON key, placeholder name). Same
// visual language as inline `code` tags used elsewhere.
function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <span style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: 4,
      padding: "1px 5px", fontFamily: "monospace", fontSize: 10.5, color: C.text,
    }}>{children}</span>
  );
}

// Multi-line code block with a Copy button in the corner. Used for the curl
// step where the whole block is already parameter-substituted and the user
// really just wants to paste it. Falls back to a hint text if the browser
// blocks clipboard writes (private mode, unsupported context).
function CodeBlockWithCopy({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Private mode / permission blocked. The user can still select the text
      // manually — no toast, don't break the flow.
    }
  };
  return (
    <div style={{ position: "relative", marginTop: 5 }}>
      <pre style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 6,
        padding: "6px 40px 6px 8px", margin: 0, fontFamily: "monospace", fontSize: 10.5,
        color: C.text, lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>{text}</pre>
      <button
        type="button"
        onClick={onCopy}
        style={{
          position: "absolute", top: 4, right: 4,
          padding: "3px 8px", borderRadius: 5,
          background: copied ? C.green : C.card,
          border: `1px solid ${copied ? C.green : C.border}`,
          color: copied ? "#fff" : C.textDim,
          fontSize: 10, fontWeight: 600, cursor: "pointer",
          transition: "background 0.15s, color 0.15s, border-color 0.15s",
        }}
        aria-label={copied ? "Copied" : "Copy"}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function Step({ n, last, children }: { n: number; last?: boolean; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, marginBottom: last ? 0 : 8 }}>
      <div style={{
        flexShrink: 0,
        width: 18, height: 18, borderRadius: 9,
        background: C.amberDim, color: C.amber,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 10.5, fontWeight: 700,
      }}>{n}</div>
      <div style={{ fontSize: 11.5, color: C.textDim, lineHeight: 1.6, minWidth: 0 }}>{children}</div>
    </div>
  );
}

// TokenCounter reproduces the counter affordance from the Facebook / Gmail
// modals — a green check when the token looks long enough to be a real
// refresh_token, plus the character count. Threshold is intentionally loose
// (>=40 chars) because Spotify's refresh_tokens are variable-length.
function TokenCounter({ length }: { length: number }) {
  if (length === 0) return null;
  const ok = length >= 40;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, color: ok ? C.green : C.textMuted }}>
      {ok && <span>✓</span>}
      <span>{length}</span>
    </span>
  );
}

function FieldLabel({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return (
    <label htmlFor={htmlFor} style={{ display: "block", fontSize: 11.5, fontWeight: 600, color: C.text, marginBottom: 5 }}>
      {children}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  padding: "9px 12px",
  fontSize: 12.5,
  color: C.text,
  outline: "none",
};
