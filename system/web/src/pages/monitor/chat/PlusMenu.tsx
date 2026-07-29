import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  Plus, Paperclip, Sparkles, ChevronRight, PenLine, Store, FolderTree,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// The composer's "+" button. Replaces the bare paperclip: attaching a file is
// now one entry in a menu that also hosts the Skills sub-menu.
//
// Layout: the trigger sits at the bottom-left of the composer, so both the menu
// and the Skills fly-out open UPWARD (bottom-anchored) and to the right — the
// composer column is 760px wide and the trigger is at its left edge, so there
// is always room for the fly-out.

export type SkillsAction = "write" | "browse" | "manage";

export function PlusMenu({
  disabled, onAttachFile, onSkillsAction,
}: {
  disabled: boolean;
  onAttachFile: () => void;
  onSkillsAction: (action: SkillsAction) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Sending disables the composer — derive the menu closed rather than closing
  // it from an effect, so a disabled composer can never render a live menu.
  const open = menuOpen && !disabled;

  const close = useCallback(() => { setMenuOpen(false); setSkillsOpen(false); }, []);

  // Outside click + Escape close the whole menu. Bound only while open so the
  // composer doesn't pay for a document listener on every keystroke.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const run = (fn: () => void) => { close(); fn(); };

  return (
    <div ref={wrapRef} style={{ position: "relative", flexShrink: 0 }}>
      <button
        onClick={() => (open ? close() : setMenuOpen(true))}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        style={{
          background: open ? "color-mix(in srgb, var(--lm-text) 8%, transparent)" : "transparent",
          border: "none",
          borderRadius: "50%", width: 34, height: 34,
          cursor: disabled ? "default" : "pointer",
          color: open ? "var(--lm-text)" : "var(--lm-text-muted)",
          opacity: disabled ? 0.5 : 0.85, transition: "opacity 0.15s, background 0.15s, transform 0.15s",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          transform: open ? "rotate(45deg)" : "none",
        }}
        onMouseEnter={(e) => { if (!disabled) { e.currentTarget.style.opacity = "1"; e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 8%, transparent)"; } }}
        onMouseLeave={(e) => { e.currentTarget.style.opacity = disabled ? "0.5" : "0.85"; if (!open) e.currentTarget.style.background = "transparent"; }}
        title="More"
        aria-label="More options"
      ><Plus size={18} /></button>

      {open && (
        <div role="menu" className="lm-pop" style={{ ...menuPanel, bottom: 42, left: 0, minWidth: 190 }}>
          <MenuItem icon={Paperclip} label="Attach file" hint="max 10 MB" onClick={() => run(onAttachFile)} />

          {/* Skills row keeps the menu open and toggles the fly-out. Hover opens
              it too so it feels like a native sub-menu; the click target stays
              for keyboard/touch. */}
          <div
            onMouseEnter={() => setSkillsOpen(true)}
            style={{ position: "relative" }}
          >
            <MenuItem
              icon={Sparkles}
              label="Skills"
              active={skillsOpen}
              trailing={<ChevronRight size={14} style={{ opacity: 0.7 }} />}
              onClick={() => setSkillsOpen((v) => !v)}
            />
            {skillsOpen && (
              <div role="menu" className="lm-pop" style={{ ...menuPanel, bottom: -6, left: "calc(100% + 6px)", minWidth: 208 }}>
                <MenuItem icon={PenLine} label="Write skill" hint="author a new SKILL.md" onClick={() => run(() => onSkillsAction("write"))} />
                <MenuItem icon={Store} label="Browse skills" hint="Autonomous skill store" onClick={() => run(() => onSkillsAction("browse"))} />
                <MenuItem icon={FolderTree} label="Manage skills" hint="installed on this runtime" onClick={() => run(() => onSkillsAction("manage"))} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const menuPanel: CSSProperties = {
  position: "absolute",
  background: "var(--lm-surface)",
  border: "1px solid var(--lm-border-hi)",
  borderRadius: 12,
  padding: 5,
  boxShadow: "0 18px 44px -18px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.35)",
  zIndex: 50,
};

function MenuItem({
  icon: Icon, label, hint, trailing, active, onClick,
}: {
  icon: LucideIcon;
  label: string;
  hint?: string;
  trailing?: React.ReactNode;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      role="menuitem"
      onClick={onClick}
      style={{
        display: "flex", alignItems: "center", gap: 9, width: "100%",
        padding: "7px 9px", borderRadius: 8,
        background: active ? "color-mix(in srgb, var(--lm-text) 7%, transparent)" : "transparent",
        border: "none", cursor: "pointer", textAlign: "left",
        color: "var(--lm-text)", transition: "background 0.12s",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 9%, transparent)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = active ? "color-mix(in srgb, var(--lm-text) 7%, transparent)" : "transparent"; }}
    >
      <Icon size={15} style={{ color: "var(--lm-amber)", flexShrink: 0 }} />
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 12.5, lineHeight: 1.3 }}>{label}</span>
        {hint && <span style={{ display: "block", fontSize: 10, color: "var(--lm-text-muted)", marginTop: 1 }}>{hint}</span>}
      </span>
      {trailing}
    </button>
  );
}
