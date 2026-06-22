import { useEffect, useState } from "react";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useCapabilities } from "@/hooks/useCapabilities";
import { Cap } from "@/pages/monitor/types";
import type { SectionId as SharedSectionId } from "@/hooks/setup/types";
import { C } from "@/components/setup/shared";
import { SettingsPanel, type SettingsSectionId } from "@/components/edit/SettingsPanel";
import { Wifi, UserCircle, Cpu, Brain, Volume2, MicVocal, MessageSquare, Globe, Link, Zap, Server } from "lucide-react";

// Local subset of the shared SectionId — EditConfig uses `stt` (Language is
// rendered under id="stt"), not `language` / `deepgram` like Setup. `runtime` is
// EditConfig-only (agent backend switch), so it's not in the shared union.
type SectionId = Extract<SharedSectionId, "device" | "wifi" | "llm" | "voice" | "face" | "tts" | "realtime" | "stt" | "channel" | "mqtt"> | "runtime";
const ICON_SIZE = 15;
const GROUP_ICON_SIZE = 15;

// `cap` declares the device capability a section's hardware needs; the section is
// hidden when the device lacks it (mirrors the Monitor nav gating). `debugOnly`
// hides the section/group unless ?debug=true.
type SectionItem = { id: SectionId; label: string; icon: React.ReactNode; debugOnly?: boolean; cap?: string };
// Settings navigation is organized as a collapsible tree (parent groups +
// indented children), mirroring the Device tree in the Monitor sidebar. Group
// `debugOnly` hides the whole group unless ?debug=true; per-child `debugOnly`
// hides individual leaves. AI Brain, Language, Voice, Realtime, Runtime,
// Channels, MQTT stay gated behind ?debug=true — typical operators only need
// Device + Wi-Fi + voice/face enrollment; deeper provider knobs stay hidden by
// default.
type SectionGroup = { group: string; label: string; icon: React.ReactNode; debugOnly?: boolean; children: SectionItem[] };

const NAV_GROUPS: SectionGroup[] = [
  {
    group: "device", label: "Device", icon: <Cpu size={GROUP_ICON_SIZE} />,
    children: [
      { id: "device", label: "General", icon: <Cpu size={ICON_SIZE} /> },
      { id: "wifi",   label: "Wi-Fi",   icon: <Wifi size={ICON_SIZE} /> },
    ],
  },
  {
    group: "ai", label: "AI", icon: <Brain size={GROUP_ICON_SIZE} />, debugOnly: true,
    children: [
      { id: "llm",      label: "AI Brain", icon: <Brain size={ICON_SIZE} /> },
      { id: "runtime",  label: "Runtime",  icon: <Server size={ICON_SIZE} /> },
      { id: "stt",      label: "Language", icon: <Globe size={ICON_SIZE} />, cap: Cap.Audio },
      { id: "tts",      label: "Voice",    icon: <Volume2 size={ICON_SIZE} />, cap: Cap.Audio },
      { id: "realtime", label: "Realtime", icon: <Zap size={ICON_SIZE} />, cap: Cap.Audio },
    ],
  },
  {
    group: "identity", label: "Identity", icon: <UserCircle size={GROUP_ICON_SIZE} />,
    children: [
      { id: "voice", label: "My Voice", icon: <MicVocal size={ICON_SIZE} />, cap: Cap.Audio },
      { id: "face",  label: "Face",     icon: <UserCircle size={ICON_SIZE} />, cap: Cap.Vision },
    ],
  },
  {
    group: "connectivity", label: "Connectivity", icon: <Link size={GROUP_ICON_SIZE} />, debugOnly: true,
    children: [
      { id: "channel", label: "Channels", icon: <MessageSquare size={ICON_SIZE} /> },
      { id: "mqtt",    label: "MQTT",     icon: <Link size={ICON_SIZE} /> },
    ],
  },
];

// Flattened view (debug + capability filtered): used for hash resolution,
// active-label lookup, and the mobile tab strip.
function visibleSections(debug: boolean, hasCap: (c: string) => boolean): SectionItem[] {
  return NAV_GROUPS
    .filter((g) => debug || !g.debugOnly)
    .flatMap((g) => g.children.filter((c) => (debug || !c.debugOnly) && (!c.cap || hasCap(c.cap))));
}

function visibleGroups(debug: boolean, hasCap: (c: string) => boolean): SectionGroup[] {
  return NAV_GROUPS
    .filter((g) => debug || !g.debugOnly)
    .map((g) => ({ ...g, children: g.children.filter((c) => (debug || !c.debugOnly) && (!c.cap || hasCap(c.cap))) }))
    .filter((g) => g.children.length > 0);
}

const isDebugMode = () => new URLSearchParams(window.location.search).get("debug") === "true";

// Collapsible sidebar group — a parent row that toggles open/closed plus its
// indented children, matching the Device tree in the Monitor sidebar (chevron
// rotates on open, amber highlight on active child/leaf).
function SettingsNavGroup({ group, activeSection, onSelect }: {
  group: SectionGroup;
  activeSection: SectionId;
  onSelect: (id: SectionId) => void;
}) {
  const hasActiveChild = group.children.some((c) => c.id === activeSection);
  const [open, setOpen] = useState(hasActiveChild);
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "8px 14px", borderRadius: 8, margin: "2px 8px",
          fontSize: 12.5, fontWeight: hasActiveChild ? 600 : 400,
          color: hasActiveChild ? C.amber : "var(--lm-text-dim)",
          background: hasActiveChild ? C.amberDim : "transparent",
          cursor: "pointer", transition: "all 0.15s",
          border: "none", width: "calc(100% - 16px)", textAlign: "left", userSelect: "none",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
          {group.icon}
          {group.label}
        </span>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}>▶</span>
      </button>
      {open && (
        <div>
          {group.children.map((child) => {
            const active = activeSection === child.id;
            return (
              <button
                key={child.id}
                onClick={() => onSelect(child.id)}
                style={{
                  display: "flex", alignItems: "center", gap: 9,
                  padding: "6px 14px 6px 32px", borderRadius: 8, margin: "1px 8px",
                  fontSize: 12, fontWeight: active ? 600 : 400,
                  color: active ? C.amber : "var(--lm-text-muted)",
                  background: active ? C.amberDim : "transparent",
                  cursor: "pointer", transition: "all 0.15s",
                  border: "none", width: "calc(100% - 16px)", textAlign: "left",
                }}
              >
                {child.icon}
                {child.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────────
//
// Page shell only: owns the sidebar tree, theme toggle, active-section state +
// URL hash, mobile tabs/footer, and document title. The settings form itself
// lives in SettingsPanel so it can be embedded in the Monitor dashboard too.

export default function EditConfig() {
  const [theme, toggleTheme, themeClass] = useTheme();
  const debug = isDebugMode();
  // Hide sections whose hardware this device lacks (DEVICE.md capabilities via
  // /api/system/info). Fail-open while caps load → no flash of an empty menu.
  const { hasCap } = useCapabilities();
  const SECTIONS = visibleSections(debug, hasCap);
  const GROUPS = visibleGroups(debug, hasCap);
  const [activeSection, setActiveSection] = useState<SectionId>(() => {
    const hash = window.location.hash.replace("#", "") as SectionId;
    return NAV_GROUPS.some((g) => g.children.some((c) => c.id === hash)) ? hash : "device";
  });

  // If the active section is for hardware this device lacks (caps loaded after
  // mount, or a deep-linked #hash to a gated section), fall back to General.
  useEffect(() => {
    if (!SECTIONS.some((s) => s.id === activeSection)) setActiveSection("device");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [SECTIONS.length, activeSection]);

  const activeSectionLabel = SECTIONS.find((s) => s.id === activeSection)?.label ?? "Settings";
  useDocumentTitle(["Settings", activeSectionLabel]);

  const scrollTo = (id: SectionId) => {
    setActiveSection(id);
    window.location.hash = id;
  };

  return (
    <div className={`lm-root lm-edit ${themeClass}`} style={{
      display: "flex", height: "100vh",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
    }}>
      <style>{`
        @media (max-width: 640px) {
          .lm-edit .lm-sidebar { display: none !important; }
          .lm-edit .lm-mobile-tabs { display: flex !important; }
          .lm-edit .lm-mobile-footer { display: block !important; }
          .lm-edit .lm-main-content { padding: 16px !important; }
        }
      `}</style>

      {/* ── Sidebar (hidden on mobile) ── */}
      <aside className="lm-sidebar" style={{
        width: 192, flexShrink: 0,
        background: C.sidebar, borderRight: `1px solid ${C.border}`,
        display: "flex", flexDirection: "column",
      }}>

        <nav style={{ padding: "10px 0", flex: 1, overflowY: "auto" }}>
          {GROUPS.map((g) => (
            <SettingsNavGroup key={g.group} group={g} activeSection={activeSection} onSelect={scrollTo} />
          ))}
        </nav>

        <div style={{ padding: "12px 16px", borderTop: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <a href="/monitor" style={{
            display: "flex", alignItems: "center", gap: 7,
            color: C.textMuted, textDecoration: "none", fontSize: 12,
            transition: "color 0.15s",
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = C.textDim)}
            onMouseLeave={(e) => (e.currentTarget.style.color = C.textMuted)}
          >
            ← Monitor
          </a>
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 4px",
          }} title={`Theme: ${theme}`}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Mobile tabs (hidden on desktop) */}
        <div className="lm-mobile-tabs" style={{
          display: "none", overflowX: "auto", gap: 4, padding: "8px 12px",
          borderBottom: `1px solid ${C.border}`, flexShrink: 0, alignItems: "center",
        }}>
          {SECTIONS.map((s) => {
            const active = activeSection === s.id;
            return (
              <button key={s.id} onClick={() => scrollTo(s.id)} style={{
                padding: "5px 10px", borderRadius: 6, fontSize: 11, fontWeight: active ? 600 : 400,
                color: active ? C.amber : C.textDim,
                background: active ? C.amberDim : "transparent",
                border: "none", cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0,
              }}>
                {s.label}
              </button>
            );
          })}
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 6px", marginLeft: "auto", flexShrink: 0,
          }}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>

        {/* Content — the reusable settings form (Save button lives inside). */}
        <SettingsPanel activeSection={activeSection as SettingsSectionId} />

        {/* Mobile footer — back to Monitor. Hidden on desktop (sidebar has it). */}
        <div className="lm-mobile-footer" style={{
          display: "none", padding: "10px 16px",
          borderTop: `1px solid ${C.border}`, background: C.sidebar, flexShrink: 0,
        }}>
          <a href="/monitor" style={{
            display: "inline-flex", alignItems: "center", gap: 7,
            color: C.textMuted, textDecoration: "none", fontSize: 13,
          }}>← Monitor</a>
        </div>
      </main>
    </div>
  );
}
