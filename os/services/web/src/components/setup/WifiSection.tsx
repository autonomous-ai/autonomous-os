import { Wifi } from "lucide-react";
import { C, ConfiguredHint, PasswordField, SectionCard, SkeletonBlock, LABEL_STYLE, INPUT_STYLE, FIELD_GAP } from "./shared";
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
  // "bytes" is jargon to a normal user, so only surface the counter once the
  // SSID actually exceeds the 802.11 limit — at that point the number is
  // actionable ("trim it down"). Below the limit we stay silent.
  const showCounter = overLimit;
  return (
    <SectionCard id="wifi" title="Wi-Fi" active={active} icon={<Wifi size={17} />}
      description="Pick the home network your device should join, then enter its password.">
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="ssid" style={LABEL_STYLE}>
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
        <ConfiguredHint label="Password" />
      ) : (
        <PasswordField label="Password" id="password" value={password} onChange={setPassword} placeholder="Wi-Fi password" />
      )}
    </SectionCard>
  );
}
