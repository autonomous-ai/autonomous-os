import type { CSSProperties } from "react";

// Shared field/button styles for the Skills modals. Kept out of ModalShell.tsx
// so that file exports components only (react-refresh/only-export-components).

export const fieldLabel: CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, letterSpacing: "0.02em",
  color: "var(--lm-text-dim)", marginBottom: 6, textTransform: "uppercase",
};

export const inputStyle: CSSProperties = {
  width: "100%", padding: "9px 11px", borderRadius: 9,
  fontSize: 13, fontFamily: "inherit", boxSizing: "border-box",
};

export const btnStyle: CSSProperties = {
  padding: "8px 16px", borderRadius: 9, fontSize: 12.5, fontWeight: 600,
};

// Hover state for the skill cards in Browse skills / Manage skills: an amber
// wash plus an amber-tinted border. Imperative rather than CSS because the cards
// are inline-styled — and shared so the two grids can't drift apart.
export function applyCardHover(el: HTMLElement, on: boolean) {
  el.style.background = on ? "color-mix(in srgb, var(--lm-amber) 10%, var(--lm-card))" : "var(--lm-card)";
  el.style.borderColor = on ? "color-mix(in srgb, var(--lm-amber) 45%, transparent)" : "var(--lm-border)";
}

// Dropdown surface shared by the composer's "+" fly-out and the "New" menu in
// the Manage skills header. The anchor (top/bottom/left/right) is the caller's.
export const menuPanel: CSSProperties = {
  position: "absolute",
  background: "var(--lm-surface)",
  border: "1px solid var(--lm-border-hi)",
  borderRadius: 12,
  padding: 5,
  boxShadow: "0 18px 44px -18px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.35)",
  zIndex: 50,
};
