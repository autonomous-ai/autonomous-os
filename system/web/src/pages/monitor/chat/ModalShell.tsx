import type { ReactNode } from "react";
import { useEffect } from "react";
import { createPortal } from "react-dom";
import { X, ArrowLeft } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useTheme } from "@/lib/useTheme";

// Shared popup shell for the Skills modals. Same visual language as the
// face-owners modals (amber-tinted header wash, lm-pop entrance, portal to
// <body> with the `lm-root ${themeClass}` re-scope so --lm-* tokens resolve).
// Pulled out here so the three Skills surfaces stay one screen of JSX each.

export function ModalShell({
  icon: Icon, title, subtitle, width = 480, onClose, onBack, children, footer, bodyPadding = 18,
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  /** Max width in px — the shell always shrinks to fit narrow viewports. */
  width?: number;
  onClose: () => void;
  /** When set, a back arrow appears left of the icon and Escape goes back
   *  instead of closing — so a drill-down doesn't dump the whole modal. */
  onBack?: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** 0 lets the body own its own padding (e.g. a full-bleed split pane). */
  bodyPadding?: number;
}) {
  const [, , themeClass] = useTheme();

  // Escape closes (or steps back). Bound on the document so it works regardless
  // of what inside the modal has focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") (onBack ?? onClose)(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, onBack]);

  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="lm-pop"
        style={{
          width: `min(${width}px, 100%)`,
          maxHeight: "min(680px, 90vh)",
          display: "flex", flexDirection: "column",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-amber) 4%, transparent), transparent 130px), var(--lm-surface)",
          border: "1px solid var(--lm-border-hi)",
          borderRadius: 14,
          boxShadow: "0 24px 64px -20px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.4)",
          overflow: "hidden",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 12, padding: "16px 18px", borderBottom: "1px solid var(--lm-border)",
          flexShrink: 0,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0 }}>
            {onBack && (
              <button
                type="button" onClick={onBack} aria-label="Back"
                className="lm-u-btn"
                style={{
                  width: 28, height: 28, borderRadius: 8, background: "var(--lm-bg)",
                  border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
                  display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                }}
              ><ArrowLeft size={14} /></button>
            )}
            <Icon size={16} style={{ color: "var(--lm-amber)", flexShrink: 0 }} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--lm-text)" }}>{title}</div>
              {subtitle && (
                <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 1 }}>{subtitle}</div>
              )}
            </div>
          </div>
          <button
            type="button" onClick={onClose} aria-label="Close"
            className="lm-u-btn"
            style={{
              width: 30, height: 30, borderRadius: 8, background: "var(--lm-bg)",
              border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
            }}
          >
            <X size={15} />
          </button>
        </div>

        <div style={{
          flex: 1, minHeight: 0,
          overflowY: bodyPadding === 0 ? "hidden" : "auto",
          display: bodyPadding === 0 ? "flex" : undefined,
          padding: bodyPadding,
        }}>{children}</div>

        {footer && (
          <div style={{
            display: "flex", justifyContent: "flex-end", gap: 8,
            padding: "12px 18px", borderTop: "1px solid var(--lm-border)",
            background: "var(--lm-bg)", flexShrink: 0,
          }}>{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}
