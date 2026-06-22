import { useState } from "react";
import { toast } from "sonner";
import { Eye, EyeOff, Copy, Check, Cpu, Fingerprint, Network } from "lucide-react";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import { C, Field, PasswordField, SectionCard, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_PAD_ONE_ICON, FIELD_GAP, ADMIN_PASSWORD_MIN } from "./shared";

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
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>{label}</label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type="text" value={displayed} readOnly
          style={{
            ...INPUT_STYLE,
            ...INPUT_READONLY_STYLE,
            padding: INPUT_PAD_ONE_ICON,
            fontFamily: "ui-monospace, monospace",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide MAC" : "Show MAC"}
          style={{
            position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

// PasswordStrength — lightweight meter under the admin password input. This
// guards a device with a camera/mic, so we nudge toward something stronger than
// the bare minimum: score on length + character-class variety, render a 3-segment
// bar + label. Purely advisory (the only hard gate is the ADMIN_PASSWORD_MIN min);
// the goal is to discourage "1111"-class passwords without hard-blocking.
//
// UX rule: RED is reserved for the one blocking state (below the min). Once the
// password is long enough to submit, every other state is advisory, so the
// hint switches to amber/green — never red — to match the fact that Next stays
// enabled. And every message is ACTION-ORIENTED ("add a number…") rather than a
// bare verdict ("Weak"), so the user always knows what to do next.
function PasswordStrength({ value }: { value: string }) {
  if (!value) return null;
  const tooShort = value.length < ADMIN_PASSWORD_MIN;
  if (tooShort) {
    return (
      <StrengthRow level={-1} color={C.red}
        message={`At least ${ADMIN_PASSWORD_MIN} characters needed (${value.length}/${ADMIN_PASSWORD_MIN}).`} />
    );
  }
  // Score variety + length on the already-valid (≥ min) password.
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^a-zA-Z0-9]/].filter((re) => re.test(value)).length;
  let score = 0;
  if (value.length >= 12) score += 1;
  if (classes >= 2) score += 1;
  if (classes >= 3) score += 1;
  // Collapse to 3 buckets: weak (0), fair (1-2), strong (3).
  const level = score === 0 ? 0 : score <= 2 ? 1 : 2;
  // Advisory messages always tell the user the concrete way to level up; the
  // "Strong" case confirms instead of nagging.
  const messages = [
    "A bit simple — add a capital letter, number, or symbol to make it stronger.",
    "Good — add a symbol or make it longer for a stronger password.",
    "Strong password.",
  ];
  const colors = [C.yellow, C.yellow, C.green];
  return <StrengthRow level={level} color={colors[level]} message={messages[level]} />;
}

// StrengthRow — the 3-segment bar + hint line. level -1 = invalid (no filled
// segments, red text); 0/1/2 fill 1/2/3 segments in the given color.
function StrengthRow({ level, color, message }: { level: number; color: string; message: string }) {
  const filled = level + 1; // -1→0, 0→1, 1→2, 2→3
  return (
    <div style={{ marginTop: -4, marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 4 }}>
        {[0, 1, 2].map((i) => (
          <div key={i} style={{
            flex: 1, height: 3, borderRadius: 2,
            background: i < filled ? color : C.border,
            transition: "background 0.2s",
          }} />
        ))}
      </div>
      <div style={{ fontSize: 12, color, marginTop: 5 }}>
        {message}
      </div>
    </div>
  );
}

// DeviceMetaCard — compact read-only "device identity" card used in edit mode.
// Groups Device ID + MAC into a single bordered surface with key/value rows and
// a thin divider, instead of two free-floating read-only inputs. Device ID gets
// a copy button; MAC keeps a reveal toggle. Purely presentational — the values
// are server-set and never edited here, so there's no input/form state to carry.
function MetaRow({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 14px", minHeight: 44 }}>
      <span style={{ flexShrink: 0, color: C.textMuted, display: "flex" }}>{icon}</span>
      <span style={{ flexShrink: 0, fontSize: 12.5, color: C.textDim, width: 84 }}>{label}</span>
      <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
        {children}
      </div>
    </div>
  );
}

function DeviceMetaCard({ deviceId, mac }: { deviceId: string; mac?: string }) {
  const [copied, setCopied] = useState(false);
  const [showMac, setShowMac] = useState(false);
  const copyId = () => {
    navigator.clipboard?.writeText(deviceId).then(() => {
      setCopied(true);
      toast.success("Device ID copied");
      setTimeout(() => setCopied(false), 1400);
    }).catch(() => {});
  };
  const maskedMac = showMac ? mac : "•".repeat(Math.min(14, (mac?.length ?? 0) || 8));
  const valueText: React.CSSProperties = {
    fontFamily: "ui-monospace, monospace", fontSize: 13, color: C.text,
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  };
  const iconBtn: React.CSSProperties = {
    flexShrink: 0, height: 28, width: 28, padding: 0, borderRadius: 7,
    background: "transparent", border: "none", cursor: "pointer",
    display: "flex", alignItems: "center", justifyContent: "center",
    color: C.textMuted, transition: "background 0.15s, color 0.15s",
  };
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ ...LABEL_STYLE, marginBottom: 8 }}>Device info</div>
      <div style={{
        border: `1px solid ${C.border}`, borderRadius: 12,
        background: C.surface, overflow: "hidden",
      }}>
        <MetaRow icon={<Fingerprint size={15} />} label="Device ID">
          <span style={valueText} title={deviceId}>{deviceId || "—"}</span>
          {deviceId && (
            <button
              type="button" onClick={copyId} tabIndex={-1}
              className="lm-eye-btn" style={iconBtn}
              aria-label="Copy Device ID" title={copied ? "Copied!" : "Copy"}
            >
              {copied ? <Check size={14} style={{ color: C.green }} /> : <Copy size={14} />}
            </button>
          )}
        </MetaRow>
        {mac && (
          <>
            <div style={{ height: 1, background: C.border }} />
            <MetaRow icon={<Network size={15} />} label="MAC">
              <span style={valueText} title={showMac ? mac : undefined}>{maskedMac}</span>
              <button
                type="button" onClick={() => setShowMac((v) => !v)} tabIndex={-1}
                className="lm-eye-btn" style={iconBtn}
                aria-label={showMac ? "Hide MAC" : "Show MAC"} title={showMac ? "Hide" : "Reveal"}
              >
                {showMac ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </MetaRow>
          </>
        )}
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
  // Description adapts to mode: setup (pick a new password) vs. edit (rotate an
  // existing one). The rotate flow has no password fields visible until the
  // operator clicks the pencil, so its copy points at that.
  const description = showAdminPasswordFields
    ? "Set an admin password — you'll use it to sign in from any browser after setup."
    : "Your device's identity and admin login.";
  return (
    <SectionCard id="device" title="Device" active={active} description={description} icon={<Cpu size={17} />}>
      {/* The admin password is the only thing the operator actively does on
          this step, so it leads. Device ID / MAC are read-only identifiers and
          drop to a compact metadata footer — putting them first made the step
          look like "nothing to do here" and operators skipped past the password. */}
      {showAdminPasswordFields && (
        <>
          <PasswordField
            label="Admin Password"
            id="admin_password"
            value={adminPassword ?? ""}
            onChange={setAdminPassword!}
            placeholder={`At least ${ADMIN_PASSWORD_MIN} characters`}
          />
          <PasswordStrength value={adminPassword ?? ""} />
          <PasswordField
            label="Confirm Password"
            id="admin_password_confirm"
            value={adminPasswordConfirm ?? ""}
            onChange={setAdminPasswordConfirm!}
            placeholder="Re-enter password"
            error={mismatch ? "Passwords don't match." : undefined}
          />
        </>
      )}
      {showRotateField && (
        <>
          <SecretUpdateField
            label="Admin Password"
            id="admin_password"
            configured={true}
            value={rotateAdminPassword ?? ""}
            onChange={setRotateAdminPassword!}
            placeholder={`New password (min ${ADMIN_PASSWORD_MIN} chars)`}
          />
          {/* Same strength meter as the setup flow — only meaningful once the
              operator starts typing a new password. Empty (the resting "keep
              current password" state) renders nothing. */}
          <PasswordStrength value={rotateAdminPassword ?? ""} />
        </>
      )}

      {/* Read-only identity metadata. Edit mode (Settings → General) groups
          Device ID + MAC into a compact key/value card with copy/reveal — these
          are server-set identifiers, never edited here. Setup mode keeps the
          plain Field/MaskedReadField so the first-run flow is unchanged. */}
      {showRotateField ? (
        <DeviceMetaCard deviceId={deviceId} mac={mac} />
      ) : (
        <>
          <Field label="Device ID" id="device_id" value={deviceId} onChange={setDeviceId} placeholder="device-001" readOnly />
          {mac && <MaskedReadField label="MAC" id="mac" value={mac} />}
        </>
      )}
    </SectionCard>
  );
}
