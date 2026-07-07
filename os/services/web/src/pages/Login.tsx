import { useState, useCallback, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { login, safeSearch } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { C, PasswordField } from "@/components/setup/shared";

// Login page — single password field that POSTs /api/login. On success the
// server sets the os_session cookie (httpOnly + SameSite=Strict), and we
// navigate back to the page the user originally tried to reach (?next=…) or
// fall back to /monitor.
export default function Login() {
  const [theme, toggleTheme, themeClass] = useTheme();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  useDocumentTitle("Sign in");

  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // `next` is captured from the URL so a bookmarked /edit lands the operator
  // back on /edit after login instead of always dumping them at /monitor.
  // Validated client-side: only same-origin pathnames are allowed (no
  // protocol-relative or absolute external URLs).
  const nextParam = searchParams.get("next") || "";
  const nextSafe =
    nextParam.startsWith("/") && !nextParam.startsWith("//") ? nextParam : "/monitor";

  const submit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setError(null);
    setBusy(true);
    try {
      await login(password);
      // Cookie is now set. Strip any secret query params before navigating.
      navigate(`${nextSafe}${safeSearch()}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }, [password, nextSafe, navigate]);

  return (
    <div className={`lm-root ${themeClass}`} style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
      padding: 24,
    }}>
      <div style={{
        width: "100%", maxWidth: 360,
        background: C.card, border: `1px solid ${C.border}`,
        borderRadius: 12, padding: "24px 22px",
        position: "relative",
      }}>
        <button onClick={toggleTheme} style={{
          position: "absolute", top: 12, right: 12,
          background: "none", border: "none", cursor: "pointer",
          fontSize: 14, color: C.textMuted, padding: "4px 6px",
        }} title={`Theme: ${theme}`}>
          {theme === "dark" ? "◑" : "◐"}
        </button>

        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 4, color: C.text }}>
          Sign in
        </div>
        <div style={{ fontSize: 12, color: C.textDim, marginBottom: 18, lineHeight: 1.5 }}>
          Enter the admin password you set during device setup.
          <div style={{ marginTop: 6 }}>
            If you haven't set one, the default is the 4 characters after the
            dash on the sticker at the bottom of your device.
          </div>
        </div>

        {error && (
          <div style={{
            background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
            borderRadius: 8, padding: "9px 12px", fontSize: 12, color: C.red, marginBottom: 14,
          }}>
            {error}
          </div>
        )}

        <form onSubmit={submit}>
          <PasswordField
            label="Admin Password"
            id="login-password"
            value={password}
            onChange={setPassword}
            placeholder="••••••••"
          />
          <button
            type="submit"
            disabled={busy || !password}
            style={{
              width: "100%", padding: "9px 14px", borderRadius: 7,
              fontSize: 13, fontWeight: 600,
              background: busy || !password ? C.surface : C.amber,
              color: busy || !password ? C.textMuted : "#0C0B09",
              border: "none", cursor: busy || !password ? "not-allowed" : "pointer",
              marginTop: 4, opacity: busy ? 0.7 : 1,
            }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
