import { useState } from "react";
import { Wifi, Eye, EyeOff, Settings } from "lucide-react";
import { C, ConfiguredHint, PasswordField, SectionCard, SkeletonBlock, LABEL_STYLE, INPUT_STYLE, INPUT_PAD_ONE_ICON, FIELD_GAP, ADMIN_PASSWORD_MIN } from "./shared";
import type { NetworkItem } from "@/types";

// Small uppercase group label that separates the Device and Wi-Fi field groups
// inside the merged (V2) card, so the single card still reads as two distinct
// sections without needing two separate cards.
function GroupLabel({ children, first = false }: { children: React.ReactNode; first?: boolean }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 700, letterSpacing: "0.06em",
      textTransform: "uppercase", color: C.textMuted,
      marginTop: first ? 0 : 18, marginBottom: 10,
      paddingTop: first ? 0 : 16,
      borderTop: first ? "none" : `1px solid ${C.border}`,
    }}>
      {children}
    </div>
  );
}

// 802.11 caps SSID at 32 bytes (not 32 chars). Each Chinese UTF-8 char is
// 3 bytes, so a short-looking SSID can still overflow. Counting bytes here
// matches the backend's len([]byte(ssid)) check in network.SetupNetwork.
const SSID_MAX_BYTES = 32;
const ssidByteLength = (s: string) => new TextEncoder().encode(s).length;

export function WifiSection({
  active, ssid, setSsid, password, setPassword, loadingList, uniqueNetworks,
  passwordConfigured = false,
  adminPassword, setAdminPassword,
}: {
  active: boolean;
  ssid: string;
  setSsid: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  loadingList: boolean;
  uniqueNetworks: NetworkItem[];
  /** True when ConfigPublicResponse.has_network_password=true: hide the
   *  password input + show "configured" indicator. Operator can rotate via
   *  /edit or by clicking "update" → toggles back into the input. */
  passwordConfigured?: boolean;
  /** Device admin password. Only passed (and only rendered) when the device
   *  has no admin password on file yet — i.e. first-time setup. Shown in clear
   *  text on purpose (no hide toggle, no confirm): it's set once here and the
   *  operator needs to see what they're typing since they'll sign in with it.
   *  Caller gates this on `!hasAdminPassword`. */
  adminPassword?: string;
  setAdminPassword?: (v: string) => void;
}) {
  const showAdminPassword = setAdminPassword !== undefined;
  // Device password is revealed by default (it's set once and the operator
  // needs to read it back), with an eye toggle to hide if someone's watching.
  const [adminVisible, setAdminVisible] = useState(true);
  const bytes = ssidByteLength(ssid);
  const overLimit = bytes > SSID_MAX_BYTES;
  // "bytes" is jargon to a normal user, so only surface the counter once the
  // SSID actually exceeds the 802.11 limit — at that point the number is
  // actionable ("trim it down"). Below the limit we stay silent.
  const showCounter = overLimit;
  return (
    // When the admin password is folded in (V2), the card covers two groups, so
    // it gets a neutral title/icon + a description that mentions both. When it's
    // just Wi-Fi (V1), it keeps the Wi-Fi title/icon and Wi-Fi-only copy.
    <SectionCard
      id="wifi"
      active={active}
      title={showAdminPassword ? "Set up your device" : "Wi-Fi"}
      icon={showAdminPassword ? <Settings size={17} /> : <Wifi size={17} />}
      description={showAdminPassword
        ? "Create a password and connect your device to Wi-Fi."
        : "Pick the home network your device should join, then enter its password."}
    >
      {/* Device admin password — shown first, in clear text. Set once here; the
          operator signs in with it later, so no hide toggle / no confirm.
          Only rendered when the caller passes setAdminPassword (V2 first-time
          setup); in V1 the password lives in the Device step instead. */}
      {showAdminPassword && (
        <>
          <GroupLabel first>Device password</GroupLabel>
          <div style={{ marginBottom: FIELD_GAP }}>
            <div style={{ position: "relative" }}>
            <input
              id="admin_password" type={adminVisible ? "text" : "password"} value={adminPassword ?? ""}
              onChange={(e) => setAdminPassword!(e.target.value)}
              placeholder={`At least ${ADMIN_PASSWORD_MIN} characters`} autoComplete="off"
              style={{ ...INPUT_STYLE, padding: INPUT_PAD_ONE_ICON }}
            />
            <button
              type="button" onClick={() => setAdminVisible((v) => !v)} tabIndex={-1}
              className="lm-eye-btn"
              aria-label={adminVisible ? "Hide password" : "Show password"}
              style={{
                position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
                height: 32, width: 32, padding: 0, background: "none", border: "none",
                cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >
              {adminVisible ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
            <div style={{ marginTop: 6, fontSize: 12, color: C.textDim, lineHeight: 1.5 }}>
              Keeps your device private and lets you sign in later. Don't lose it.
            </div>
          </div>
          <GroupLabel>Wi-Fi</GroupLabel>
        </>
      )}
      <div style={{ marginBottom: FIELD_GAP }}>
        {!showAdminPassword && (
          <label htmlFor="ssid" style={LABEL_STYLE}>
            Wi-Fi network
          </label>
        )}
        {loadingList ? (
          <SkeletonBlock />
        ) : uniqueNetworks.length > 0 ? (
          <select
            id="ssid"
            value={ssid}
            onChange={(e) => setSsid(e.target.value)}
            style={{
              ...INPUT_STYLE,
              border: `1px solid ${overLimit ? C.red : C.border}`,
              cursor: "pointer",
            }}
          >
            <option value="">Select network</option>
            {uniqueNetworks.map((n) => (
              <option key={n.bssid} value={n.ssid}>{n.ssid}</option>
            ))}
          </select>
        ) : (
          <input
            id="ssid" type="text" value={ssid}
            onChange={(e) => setSsid(e.target.value)}
            placeholder="Enter Wi-Fi name" autoComplete="off"
            style={{
              ...INPUT_STYLE,
              border: `1px solid ${overLimit ? C.red : C.border}`,
            }}
          />
        )}
        {showCounter && (
          <div style={{
            marginTop: 6, fontSize: 12,
            color: overLimit ? C.red : C.textDim,
          }}>
            {overLimit
              ? `SSID too long: ${bytes}/${SSID_MAX_BYTES} bytes (802.11 limit)`
              : `${bytes}/${SSID_MAX_BYTES} bytes`}
          </div>
        )}
      </div>
      {passwordConfigured ? (
        <ConfiguredHint label={showAdminPassword ? "" : "Wi-Fi password"} />
      ) : (
        <PasswordField label={showAdminPassword ? "" : "Wi-Fi password"} id="password" value={password} onChange={setPassword} placeholder="Wi-Fi password" />
      )}
    </SectionCard>
  );
}
