import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import { C, Field, PasswordField, SectionCard } from "./shared";

// Read-only MAC field masked behind ••••, with an eye toggle to reveal. The
// caller only renders this when `value` is non-empty — on the pre-auth Setup
// page, GET /api/device/config is admin-gated and returns 401, so MAC stays
// empty and the field is omitted entirely rather than showing "not available".
function MaskedReadField({ label, id, value }: {
  label: string; id: string; value: string;
}) {
  const [show, setShow] = useState(false);
  const displayed = show ? value : "•".repeat(Math.min(12, value.length || 8));
  return (
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>{label}</label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type="text" value={displayed} readOnly
          style={{
            width: "100%", boxSizing: "border-box",
            background: C.bg, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 38px 8px 11px",
            fontSize: 12.5, color: C.textDim,
            outline: "none", cursor: "default",
            fontFamily: "ui-monospace, monospace",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          aria-label={show ? "Hide MAC" : "Show MAC"}
          style={{
            position: "absolute", right: 0, top: 0, height: "100%",
            padding: "0 11px", background: "none", border: "none",
            color: C.textMuted, cursor: "pointer",
            display: "flex", alignItems: "center",
          }}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

export function DeviceSection({
  active, deviceId, setDeviceId, mac,
  adminPassword, setAdminPassword,
  adminPasswordConfirm, setAdminPasswordConfirm,
  rotateAdminPassword, setRotateAdminPassword,
}: {
  active: boolean;
  deviceId: string;
  setDeviceId: (v: string) => void;
  mac?: string;
  // Setup mode — operator picks an initial password (with confirm). Caller
  // gates these on `!hasAdminPassword`.
  adminPassword?: string;
  setAdminPassword?: (v: string) => void;
  adminPasswordConfirm?: string;
  setAdminPasswordConfirm?: (v: string) => void;
  // EditConfig mode — write-only rotate field. Empty value means "keep
  // existing hash"; submit only ships admin_password when the operator typed
  // something here. Server bcrypts + replaces; live sessions keep working.
  rotateAdminPassword?: string;
  setRotateAdminPassword?: (v: string) => void;
}) {
  const showAdminPasswordFields = setAdminPassword !== undefined;
  const showRotateField = setRotateAdminPassword !== undefined;
  const mismatch =
    showAdminPasswordFields &&
    !!adminPasswordConfirm &&
    !!adminPassword &&
    adminPassword !== adminPasswordConfirm;
  return (
    <SectionCard id="device" title="Device" active={active}>
      <Field label="Device ID" id="device_id" value={deviceId} onChange={setDeviceId} placeholder="lamp-001" readOnly />
      {mac && <MaskedReadField label="MAC" id="mac" value={mac} />}
      {showAdminPasswordFields && (
        <>
          <div style={{
            fontSize: 11, color: C.textDim, marginTop: 4, marginBottom: 8, lineHeight: 1.5,
          }}>
            Set an admin password — you'll sign in with this from any browser
            after setup.
          </div>
          <PasswordField
            label="Admin Password"
            id="admin_password"
            value={adminPassword ?? ""}
            onChange={setAdminPassword!}
            placeholder="At least 6 characters"
          />
          <PasswordField
            label="Confirm Password"
            id="admin_password_confirm"
            value={adminPasswordConfirm ?? ""}
            onChange={setAdminPasswordConfirm!}
            placeholder="Re-enter password"
          />
          {mismatch && (
            <div style={{ fontSize: 11, color: C.red, marginTop: -4, marginBottom: 8 }}>
              Passwords don't match.
            </div>
          )}
        </>
      )}
      {showRotateField && (
        <SecretUpdateField
          label="Admin Password"
          id="admin_password"
          configured={true}
          value={rotateAdminPassword ?? ""}
          onChange={setRotateAdminPassword!}
          placeholder="New password (min 6 chars)"
        />
      )}
    </SectionCard>
  );
}
