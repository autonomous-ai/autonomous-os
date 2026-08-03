import { useCallback, useEffect, useRef, useState } from "react";
import {
  Plus, Paperclip, Sparkles, ChevronRight, PenLine, Store, FolderTree, Upload,
} from "lucide-react";
import { MenuDivider, MenuItem } from "./MenuPanel";
import { menuPanel } from "./styles";

// The composer's "+" button. Replaces the bare paperclip: attaching a file is
// now one entry in a menu that also hosts the Skills sub-menu.
//
// Layout: the trigger sits at the bottom-left of the composer, so both the menu
// and the Skills fly-out open UPWARD (bottom-anchored) and to the right — the
// composer column is 760px wide and the trigger is at its left edge, so there
// is always room for the fly-out.

export type SkillsAction = "write" | "upload" | "browse" | "manage";

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
                <MenuItem icon={Upload} label="Upload a skill" hint=".skill / .zip / .md from this computer" onClick={() => run(() => onSkillsAction("upload"))} />
                {/* Splits "add one of your own" from "work with what's out there
                    / already installed". */}
                <MenuDivider />
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

