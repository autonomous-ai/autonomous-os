import { useEffect, useRef, useState } from "react";
import { Pencil, X, Eye, EyeOff } from "lucide-react";
import type { SectionId } from "@/hooks/setup/types";

// ── CSS vars ──────────────────────────────────────────────────────────────────

export const C = {
  bg:        "var(--lm-bg)",
  sidebar:   "var(--lm-sidebar)",
  card:      "var(--lm-card)",
  surface:   "var(--lm-surface)",
  border:    "var(--lm-border)",
  amber:     "var(--lm-amber)",
  amberDim:  "var(--lm-amber-dim)",
  text:      "var(--lm-text)",
  textDim:   "var(--lm-text-dim)",
  textMuted: "var(--lm-text-muted)",
  red:       "var(--lm-red)",
  green:     "var(--lm-green)",
};

// ── small components ──────────────────────────────────────────────────────────

export function Field({
  label, id, value, onChange, placeholder, type = "text", readOnly = false, required = false,
}: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; type?: string; readOnly?: boolean; required?: boolean;
}) {
  const [focused, setFocused] = useState(false);
  return (
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
        {label}
      </label>
      <input
        id={id} type={type} value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} autoComplete="off"
        readOnly={readOnly} required={required}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        style={{
          width: "100%", boxSizing: "border-box",
          background: readOnly ? C.bg : C.surface,
          border: `1px solid ${focused && !readOnly ? C.amber : C.border}`,
          borderRadius: 7, padding: "8px 11px",
          fontSize: 12.5, color: readOnly ? C.textDim : C.text, outline: "none",
          cursor: readOnly ? "default" : "text",
          transition: "border-color 0.15s",
        }}
      />
    </div>
  );
}

export function PasswordField({ label, id, value, onChange, placeholder, readOnly = false }: {
  label: string; id: string; value: string;
  onChange: (v: string) => void; placeholder?: string; readOnly?: boolean;
}) {
  const [show, setShow] = useState(false);
  const [focused, setFocused] = useState(false);
  return (
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
        {label}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: readOnly ? C.bg : C.surface,
            border: `1px solid ${focused && !readOnly ? C.amber : C.border}`,
            borderRadius: 7, padding: "8px 38px 8px 11px",
            fontSize: 12.5, color: readOnly ? C.textDim : C.text, outline: "none",
            cursor: readOnly ? "default" : "text",
            transition: "border-color 0.15s",
          }}
        />
        <button type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
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
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
        {label}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type={type} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            width: "100%", boxSizing: "border-box",
            background: readOnly ? C.bg : C.surface,
            border: `1px solid ${C.border}`,
            borderRadius: 7, padding: showToggle ? "8px 36px 8px 11px" : "8px 11px",
            fontSize: 12.5, color: readOnly ? C.textDim : C.text, outline: "none",
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
              padding: "0 10px", background: "none", border: "none",
              color: readOnly ? C.amber : C.textMuted, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {readOnly ? <Pencil size={13} /> : <X size={14} />}
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
  const rightPad = showToggle ? 64 : 38;
  return (
    <div style={{ marginBottom: 12 }}>
      <label htmlFor={id} style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
        {label}
      </label>
      <div style={{ position: "relative" }}>
        <input
          id={id} type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder} autoComplete="off"
          readOnly={readOnly} required={required}
          style={{
            width: "100%", boxSizing: "border-box",
            background: readOnly ? C.bg : C.surface,
            border: `1px solid ${C.border}`,
            borderRadius: 7, padding: `8px ${rightPad}px 8px 11px`,
            fontSize: 12.5, color: readOnly ? C.textDim : C.text, outline: "none",
            cursor: readOnly ? "default" : "text",
          }}
        />
        <button
          type="button" onClick={() => setShow((v) => !v)} tabIndex={-1}
          style={{
            position: "absolute", right: showToggle ? 28 : 0, top: 0, height: "100%",
            padding: "0 10px", background: "none", border: "none",
            color: C.textMuted, cursor: "pointer",
            display: "flex", alignItems: "center",
          }}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
        {showToggle && (
          <button
            type="button"
            onClick={readOnly ? unlock : handleCancel}
            tabIndex={-1}
            aria-label={readOnly ? "Edit" : "Cancel edit"}
            title={readOnly ? "Edit" : "Cancel edit"}
            style={{
              position: "absolute", right: 0, top: 0, height: "100%",
              padding: "0 10px", background: "none", border: "none",
              color: readOnly ? C.amber : C.textMuted, cursor: "pointer",
              display: "flex", alignItems: "center",
            }}
          >
            {readOnly ? <Pencil size={13} /> : <X size={14} />}
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
export function ConfiguredHint({ label, editPath = "/edit" }: { label: string; editPath?: string }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <label style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>{label}</label>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 10, padding: "8px 11px",
        background: C.bg, border: `1px solid ${C.border}`,
        borderRadius: 7, fontSize: 12.5, color: C.textDim,
      }}>
        <span><span style={{ color: C.green }}>✓</span>&nbsp;configured</span>
        <a href={editPath} style={{ color: C.amber, textDecoration: "none", fontSize: 11.5 }}>
          update →
        </a>
      </div>
    </div>
  );
}

export function SectionCard({ id, title, active, children }: { id: SectionId; title: string; active: boolean; children: React.ReactNode }) {
  if (!active) return null;
  return (
    <div
      id={`section-${id}`}
      style={{
        background: C.card, border: `1px solid ${C.border}`,
        borderRadius: 12, padding: "18px 20px", marginBottom: 16,
      }}
    >
      <div style={{
        fontSize: 10, fontWeight: 700, color: C.textDim,
        textTransform: "uppercase", letterSpacing: "0.09em", marginBottom: 16,
      }}>
        {title}
      </div>
      {children}
    </div>
  );
}

export function SkeletonBlock() {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: "18px 20px", marginBottom: 16 }}>
      <div style={{ width: 80, height: 8, borderRadius: 6, background: C.surface, marginBottom: 14 }} />
      <div style={{ width: "100%", height: 32, borderRadius: 6, background: C.surface, marginBottom: 10 }} />
    </div>
  );
}
