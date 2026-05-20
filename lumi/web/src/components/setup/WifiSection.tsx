import { C, ConfiguredHint, PasswordField, SectionCard, SkeletonBlock } from "./shared";
import type { NetworkItem } from "@/types";

// 802.11 caps SSID at 32 bytes (not 32 chars). Each Chinese UTF-8 char is
// 3 bytes, so a short-looking SSID can still overflow. Counting bytes here
// matches the backend's len([]byte(ssid)) check in network.SetupNetwork.
const SSID_MAX_BYTES = 32;
const ssidByteLength = (s: string) => new TextEncoder().encode(s).length;

export function WifiSection({
  active, ssid, setSsid, password, setPassword, loadingList, uniqueNetworks,
  passwordConfigured = false,
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
}) {
  const bytes = ssidByteLength(ssid);
  const overLimit = bytes > SSID_MAX_BYTES;
  const showCounter = bytes > 0 && (overLimit || bytes !== ssid.length);
  return (
    <SectionCard id="wifi" title="Wi-Fi" active={active}>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="ssid" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
          Wi-Fi network
        </label>
        {loadingList ? (
          <SkeletonBlock />
        ) : uniqueNetworks.length > 0 ? (
          <select
            id="ssid"
            value={ssid}
            onChange={(e) => setSsid(e.target.value)}
            style={{
              width: "100%", boxSizing: "border-box",
              background: C.surface,
              border: `1px solid ${overLimit ? C.red : C.border}`,
              borderRadius: 7, padding: "8px 11px",
              fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
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
              width: "100%", boxSizing: "border-box",
              background: C.surface,
              border: `1px solid ${overLimit ? C.red : C.border}`,
              borderRadius: 7, padding: "8px 11px",
              fontSize: 12.5, color: C.text, outline: "none",
            }}
          />
        )}
        {showCounter && (
          <div style={{
            marginTop: 5, fontSize: 11,
            color: overLimit ? C.red : C.textDim,
          }}>
            {overLimit
              ? `SSID too long: ${bytes}/${SSID_MAX_BYTES} bytes (802.11 limit)`
              : `${bytes}/${SSID_MAX_BYTES} bytes`}
          </div>
        )}
      </div>
      {passwordConfigured ? (
        <ConfiguredHint label="Password" />
      ) : (
        <PasswordField label="Password" id="password" value={password} onChange={setPassword} placeholder="Wi-Fi password" />
      )}
    </SectionCard>
  );
}
