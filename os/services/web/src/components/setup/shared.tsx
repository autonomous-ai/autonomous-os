import { useEffect, useRef, useState } from "react";
import { Pencil, X, Eye, EyeOff } from "lucide-react";
import type { SectionId } from "@/hooks/setup/types";
import { C, FIELD_GAP, ADMIN_PASSWORD_MIN, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_FOCUS_SHADOW, INPUT_ERROR_SHADOW, INPUT_PAD_ONE_ICON, INPUT_PAD_TWO_ICONS } from "./styles";

// Re-export the design tokens so existing `import { C } from "./shared"` sites
// keep working — the source of truth now lives in ./styles (a plain module, so
// it doesn't trip react-refresh's component-only-export rule).
export { C, FIELD_GAP, ADMIN_PASSWORD_MIN, LABEL_STYLE, INPUT_STYLE, INPUT_READONLY_STYLE, INPUT_FOCUS_SHADOW, INPUT_ERROR_SHADOW, INPUT_PAD_ONE_ICON, INPUT_PAD_TWO_ICONS };

// FieldError — the red message line shown under an invalid input. Centralised so
// every field renders errors identically (icon + 12px red text). Returns null
// when there's no error so callers can render it unconditionally.
export function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <div style={{
      fontSize: 12, color: C.red, marginTop: 6,
      display: "flex", alignItems: "center", gap: 5,
    }}>
      <span aria-hidden>⚠</span>{message}
    </div>
  );
}

// ── small components ──────────────────────────────────────────────────────────

export function Field({
  label, id, value, onChange, placeholder, type = "text", readOnly = false, required = false, error,
}: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; type?: string; readOnly?: boolean; required?: boolean;
  /** When set, the input shows the error (red border + glow) state and renders
   *  the message below. Error styling wins over focus styling. */
  error?: string;
}) {
  const [focused, setFocused] = useState(false);
  const hasError = !!error && !readOnly;
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>
        {label}
      </label>
      <input
        id={id} type={type} value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} autoComplete="off"
        readOnly={readOnly} required={required}
        aria-invalid={hasError || undefined}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          ...INPUT_STYLE,
          border: `1px solid ${borderColor(hasError, focused && !readOnly)}`,
          boxShadow: boxShadowFor(hasError, focused && !readOnly),
          ...(readOnly ? INPUT_READONLY_STYLE : null),
        }}
      />
      <FieldError message={hasError ? error : undefined} />
    </div>
  );
}

export function PasswordField({ label, id, value, onChange, placeholder, readOnly = false, error }: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; readOnly?: boolean;
  /** See Field.error — same red border/glow + message behaviour. */
  error?: string;
}) {
  const [show, setShow] = useState(false);
  const [focused, setFocused] = useState(false);
  const hasError = !!error && !readOnly;
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && (
        <label htmlFor={id} style={LABEL_STYLE}>
          {label}
        </label>
      )}
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly}
          aria-invalid={hasError || undefined}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            border: `1px solid ${borderColor(hasError, focused && !readOnly)}`,
            boxShadow: boxShadowFor(hasError, focused && !readOnly),
            padding: INPUT_PAD_ONE_ICON,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        <button type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide password" : "Show password"}
          style={{
            position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </div>
      <FieldError message={hasError ? error : undefined} />
    </div>
  );
}

// Border / glow resolution shared by the editable inputs. Error wins over focus,
// which wins over the resting state — so an invalid field stays red even while
// focused, and a valid focused field lights amber.
function borderColor(hasError: boolean, focused: boolean): string {
  if (hasError) return C.red;
  if (focused) return C.amber;
  return C.border;
}
function boxShadowFor(hasError: boolean, focused: boolean): string {
  if (hasError) return INPUT_ERROR_SHADOW;
  if (focused) return INPUT_FOCUS_SHADOW;
  return "none";
}

// useLockToggle — shared lock/unlock + cancel-restore logic for LockedField and
// LockedPasswordField. Captures the value when a field first becomes locked so
// "Cancel" can revert any in-progress edits.
function useLockToggle(lockedInitially: boolean, value: string, onChange: (v: string) => void) {
  const [unlocked, setUnlocked] = useState(false);
  const originalRef = useRef<string | null>(null);
  useEffect(() => {
    if (lockedInitially && originalRef.current === null) {
      originalRef.current = value;
    }
  }, [lockedInitially, value]);
  const readOnly = lockedInitially && !unlocked;
  const handleCancel = () => {
    if (originalRef.current !== null) onChange(originalRef.current);
    setUnlocked(false);
  };
  return { readOnly, showToggle: lockedInitially, unlock: () => setUnlocked(true), handleCancel };
}

export function LockedField({
  lockedInitially, label, id, value, onChange, placeholder, type = "text", required = false,
}: {
  lockedInitially: boolean; label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; type?: string; required?: boolean;
}) {
  const { readOnly, showToggle, unlock, handleCancel } = useLockToggle(lockedInitially, value, onChange);
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      <label htmlFor={id} style={LABEL_STYLE}>
        {label}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type={type} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            padding: showToggle ? INPUT_PAD_ONE_ICON : INPUT_STYLE.padding,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        {showToggle && (
          <button
            type="button"
            onClick={readOnly ? unlock : handleCancel}
            tabIndex={-1}
            aria-label={readOnly ? "Edit" : "Cancel edit"}
            title={readOnly ? "Edit" : "Cancel edit"}
            style={{
              position: "absolute", right: 0, top: 0, height: "100%",
              padding: "0 12px", background: "none", border: "none",
              color: readOnly ? C.amber : C.textMuted, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {readOnly ? <Pencil size={14} /> : <X size={15} />}
          </button>
        )}
      </div>
    </div>
  );
}

export function LockedPasswordField({
  lockedInitially, label, id, value, onChange, placeholder, required = false,
}: {
  lockedInitially: boolean; label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; required?: boolean;
}) {
  const [show, setShow] = useState(false);
  const { readOnly, showToggle, unlock, handleCancel } = useLockToggle(lockedInitially, value, onChange);
  // Right side stack: [show/hide][lock toggle]. show/hide is always available so
  // the user can verify a saved password without unlocking it for edit first.
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && (
        <label htmlFor={id} style={LABEL_STYLE}>
          {label}
        </label>
      )}
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            ...INPUT_STYLE,
            background: readOnly ? C.bg : C.surface,
            padding: showToggle ? INPUT_PAD_TWO_ICONS : INPUT_PAD_ONE_ICON,
            color: readOnly ? C.textDim : C.text,
            cursor: readOnly ? "default" : "text",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          className="lm-eye-btn"
          aria-label={show ? "Hide" : "Show"}
          style={{
            position: "absolute", right: showToggle ? 37 : 5, top: "50%", transform: "translateY(-50%)",
            height: 32, width: 32, padding: 0, background: "none", border: "none",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          {show ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
        {showToggle && (
          <button
            type="button"
            onClick={readOnly ? unlock : handleCancel}
            tabIndex={-1}
            className="lm-eye-btn"
            aria-label={readOnly ? "Edit" : "Cancel edit"}
            title={readOnly ? "Edit" : "Cancel edit"}
            style={{
              position: "absolute", right: 5, top: "50%", transform: "translateY(-50%)",
              height: 32, width: 32, padding: 0, background: "none", border: "none",
              color: readOnly ? C.amber : undefined, cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            {readOnly ? <Pencil size={14} /> : <X size={15} />}
          </button>
        )}
      </div>
    </div>
  );
}

// ConfiguredHint renders a "✓ configured" row for a secret field that the
// server already has on file. Used when ConfigPublicResponse reports
// `has_*=true` — instead of showing an empty + locked password input (the
// raw value isn't returned anymore), we hide the input entirely and tell the
// operator to rotate via /edit. Keeps the Setup form short on re-setup.
export function ConfiguredHint({ label, editPath = "/setting" }: { label: string; editPath?: string }) {
  return (
    <div style={{ marginBottom: FIELD_GAP }}>
      {label && <label style={LABEL_STYLE}>{label}</label>}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 10, padding: "10px 13px",
        background: C.bg, border: `1px solid ${C.border}`,
        borderRadius: 10, fontSize: 14, color: C.textDim,
      }}>
        <span><span style={{ color: C.green }}>✓</span>&nbsp;configured</span>
        <a href={editPath} style={{ color: C.amber, textDecoration: "none", fontSize: 13 }}>
          update →
        </a>
      </div>
    </div>
  );
}

export function SectionCard({ id, title, description, icon, active, children }: {
  // EditConfig-only sections (e.g. "runtime", "timezone") aren't part of the
  // Setup SectionId union; the id is only used as a DOM anchor, so accept those too.
  id: SectionId | "runtime" | "timezone"; title: string; description?: string; icon?: React.ReactNode; active: boolean; children: React.ReactNode;
}) {
  // Stay mounted when inactive (display:none) so form inputs keep their
  // refs and any controlled state remains live. Sidebar tabs gate visibility
  // only; URL query params + parent useState still drive submitted values
  // even when the section isn't on screen. Matches the `?debug=true/false`
  // contract: hide from view, don't unmount.
  return (
    <div
      id={`section-${id}`}
      // lm-card adds elevation + hover. The section stays mounted when inactive
      // (display:none) to preserve input refs/state — see the note below — so we
      // can't remount to replay the entry animation. The CSS animation still
      // plays once on first mount, which covers the common forward-walk case.
      className="lm-card lm-fade-in"
      style={{
        display: active ? "block" : "none",
        padding: "22px 24px", marginBottom: 16,
      }}
    >
      {/* Header row: an amber-tinted icon chip anchors the step visually, with
          the title (and optional one-line description) stacked beside it. The
          chip reuses the same lucide icon shown in the sidebar for that step. */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        {icon && (
          <div style={{
            flexShrink: 0,
            width: 36, height: 36, borderRadius: 10,
            background: C.amberDim, color: C.amber,
            display: "flex", alignItems: "center", justifyContent: "center",
            // Soft amber ring for a touch of depth; theme-var so it flips.
            boxShadow: "inset 0 0 0 1px var(--lm-amber-glow)",
          }}>
            {icon}
          </div>
        )}
        <div style={{ minWidth: 0, paddingTop: icon ? 1 : 0 }}>
          <div style={{
            fontSize: 14.5, fontWeight: 600, color: C.text, lineHeight: 1.3,
          }}>
            {title}
          </div>
          {/* One-line, consistent step description. Replaces the ad-hoc intro
              lines a few sections rolled their own — pass it via `description`
              so every step reads the same way. */}
          {description && (
            <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.5, marginTop: 4 }}>
              {description}
            </div>
          )}
        </div>
      </div>
      {children}
    </div>
  );
}

export function SkeletonBlock() {
  return (
    <div className="lm-card" style={{ padding: "20px 22px", marginBottom: 16 }}>
      <div style={{ width: 80, height: 8, borderRadius: 6, background: C.surface, marginBottom: 14 }} />
      <div style={{ width: "100%", height: 32, borderRadius: 6, background: C.surface, marginBottom: 10 }} />
    </div>
  );
}
