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
