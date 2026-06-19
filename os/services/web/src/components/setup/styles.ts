import type { CSSProperties } from "react";

// ── shared CSS vars ─────────────────────────────────────────────────────────
// The `--lm-*` design tokens, referenced as `C.x` throughout the setup/edit
// wizard. Kept in this plain .ts module (no components) so importing them
// doesn't trip react-refresh's "only export components" rule the way a shared
// .tsx file would.

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
  yellow:    "var(--lm-yellow)",
};

// ── shared sizing tokens ────────────────────────────────────────────────────
// One place to tune the wizard's typographic scale. The original values ran
// small (label 11px, input 12.5px); these bump everything up one comfortable
// step (label 13px, input 14px, roomier padding) so the form reads easily
// without losing the compact wizard feel. Every field component pulls from
// these so the scale stays consistent — change here, change everywhere.
export const FIELD_GAP = 14;

// Minimum admin/device password length. Frontend-only policy — the backend
// bcrypts any non-empty value (see internal/device/service.go), so this is the
// single knob for the floor enforced in the Setup wizard. One source of truth
// so the validation gate, the strength meter, and the field copy never drift.
export const ADMIN_PASSWORD_MIN = 4;

export const LABEL_STYLE: CSSProperties = {
  display: "block",
  fontSize: 13,
  fontWeight: 500,
  letterSpacing: "0.01em",
  color: C.textDim,
  marginBottom: 6,
};

export const INPUT_STYLE: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: "10px 13px",
  fontSize: 14,
  color: C.text,
  // Amber caret matches the accent + focus glow so the blinking cursor reads as
  // part of the same design language instead of a stray white bar.
  caretColor: C.amber,
  outline: "none",
  transition: "border-color 0.15s, box-shadow 0.15s",
};

// Readonly / inert fields (Device ID, MAC) render as a flat "info card", not an
// editable box: no border, sunken background, transparent caret so clicking
// never shows a blinking cursor that implies it's typeable. Spread AFTER
// INPUT_STYLE to override.
export const INPUT_READONLY_STYLE: CSSProperties = {
  background: C.bg,
  border: "1px solid transparent",
  color: C.textDim,
  caretColor: "transparent",
  cursor: "default",
};

// Soft amber focus ring. Applied as box-shadow on focused inputs (alongside the
// amber border) for a higher-end "lit up" feel than a bare border swap. Uses the
// existing --lm-amber-glow token so it tracks the theme.
export const INPUT_FOCUS_SHADOW = "0 0 0 3px var(--lm-amber-glow)";

// Error ring — the red counterpart to the focus glow. Shown on invalid inputs
// regardless of focus so the field stays visibly flagged until corrected. Built
// from the red token at low alpha to match the soft-ring look.
export const INPUT_ERROR_SHADOW = "0 0 0 3px var(--lm-red-glow)";

// Success ring — the green counterpart, available for "ready/ok" affordances
// (e.g. enroll buttons in a ready state). Tracks the theme via the green token.
export const INPUT_SUCCESS_SHADOW = "0 0 0 3px var(--lm-green-glow)";

// Right padding presets for inputs with trailing icon buttons (eye / pencil).
// Single button (eye OR pencil) vs. stacked (eye + pencil). Derived from the
// 13px base left padding + icon hit-area widths.
export const INPUT_PAD_ONE_ICON = "10px 42px 10px 13px";
export const INPUT_PAD_TWO_ICONS = "10px 70px 10px 13px";
