import { SecretUpdateField } from "@/components/SecretUpdateField";
import { C, Field, PasswordField, SectionCard } from "./shared";

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
      <Field label="Device ID" id="device_id" value={deviceId} onChange={setDeviceId} placeholder="lumi-001" readOnly />
      <Field label="MAC" id="mac" value={mac ?? ""} onChange={() => {}} placeholder="Lumi-XXXX" readOnly />
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
