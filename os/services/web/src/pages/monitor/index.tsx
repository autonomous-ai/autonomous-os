declare const __WEB_VERSION__: string;
import { useCallback, useEffect, useRef, useState } from "react";
import { useTheme } from "@/lib/useTheme";
import { usePolling } from "../../hooks/usePolling";
import { useEventSource } from "../../hooks/useEventSource";
import { useDocumentTitle } from "../../hooks/useDocumentTitle";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";

import { S } from "./styles";
import { API, HW, HISTORY_LEN, FLOW_EVENTS_MAX, NAV, isNavGroup, isNavLink, Cap } from "./types";
import type { Section, SystemInfo, NetworkInfo, HWHealth, OCStatus, PresenceInfo, VoiceStatus, ServoState, DisplayState, AudioVolume, LEDColor, SceneInfo, MonitorEvent, DisplayEvent, NavEntry } from "./types";
import { OverviewSection } from "./OverviewSection";
import { SystemSection } from "./SystemSection";
import { FlowSection } from "./FlowSection";
import { SensingSection } from "./SensingSection";
import { CameraSection } from "./CameraSection";
import { ServoSection } from "./ServoSection";
import { AnalyticsSection } from "./AnalyticsSection";
import { LogsSection } from "./LogsSection";
import { ChatSection } from "./ChatSection";
import { FaceOwnersSection } from "./FaceOwnersSection";
import { BluetoothSection } from "./BluetoothSection";
import { CliSection } from "./CliSection";

ChartJS.register(CategoryScale, LinearScale, BarElement, PointElement, LineElement, Title, Tooltip, Legend, Filler);

// Sections rendered as full-bleed iframes — they need their own padding/overflow override.
const EMBED_SECTIONS = new Set<Section>(["api-docs", "agent-config"]);

// Sections shown to non-debug users. Append `?debug=true` to the URL to reveal
// the full menu (Sensing, Analytics, Servo, Logs, CLI, API Docs, Agent gateway).
const PUBLIC_SECTIONS = new Set<Section>(["chat", "overview", "system", "flow", "camera", "face-owners", "bluetooth"]);

// The capability a section requires, read from its NAV leaf (single source: the
// nav definition itself declares `cap`). undefined → no hardware dependency, the
// section is always shown.
function sectionCap(id: Section): string | undefined {
  for (const entry of NAV) {
    if (isNavGroup(entry)) {
      const child = entry.children.find((c) => !isNavLink(c) && c.id === id);
      if (child && !isNavLink(child)) return child.cap;
    } else if (entry.id === id) return entry.cap;
  }
  return undefined;
}

const iframeStyle: React.CSSProperties = {
  width: "100%",
  height: "100%",
  border: "none",
  display: "block",
  background: "var(--lm-card)",
};

function allNavLeaves(): { id: Section; label: string; icon: string }[] {
  const leaves: { id: Section; label: string; icon: string }[] = [];
  for (const entry of NAV) {
    if (isNavGroup(entry)) entry.children.forEach((c) => { if (!isNavLink(c)) leaves.push(c); });
    else leaves.push(entry);
  }
  // Agent config isn't in NAV (rendered by AgentGWMenu) — register it here
  // so hash routing + topbar title work for the embedded view.
  leaves.push({ id: "agent-config", label: "Agent Config", icon: "◈" });
  return leaves;
}

function NavGroupItem({ entry, section, setSection, closeSidebar }: {
  entry: Extract<NavEntry, { group: string }>;
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
}) {
  const hasActiveChild = entry.children.some((c) => !isNavLink(c) && c.id === section);
  const [open, setOpen] = useState(hasActiveChild);
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{ ...S.navGroupHeader(hasActiveChild), display: "flex", alignItems: "center", justifyContent: "space-between" }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ fontSize: 14, lineHeight: 1 }}>{entry.icon}</span>
          {entry.label}
        </span>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}>▶</span>
      </button>
      {open && (
        <div>
          {entry.children.map((child) =>
            isNavLink(child) ? (
              <a
                key={child.href}
                href={child.href}
                style={S.navSubItem(false)}
                target={child.external ? "_blank" : undefined}
                rel={child.external ? "noreferrer" : undefined}
                onClick={closeSidebar}
              >
                <span style={{ fontSize: 13, lineHeight: 1 }}>{child.icon}</span>
                {child.label}
              </a>
            ) : (
              <a
                key={child.id}
                href={`#${child.id}`}
                style={S.navSubItem(section === child.id)}
                onClick={(e) => { e.preventDefault(); setSection(child.id); closeSidebar(); }}
              >
                <span style={{ fontSize: 13, lineHeight: 1 }}>{child.icon}</span>
                {child.label}
              </a>
            )
          )}
        </div>
      )}
    </div>
  );
}

function AgentGWMenu({ section, setSection, closeSidebar }: {
  section: Section;
  setSection: (s: Section) => void;
  closeSidebar: () => void;
}) {
  const hasActive = section === "agent-config";
  const [open, setOpen] = useState(hasActive);
  // OpenClaw Control UI 5.2 sets X-Frame-Options: DENY so we open in a new
  // tab. The gateway auth token used to ride along as a `#token=…` fragment
  // fetched from /api/agent/config-json — that endpoint is now
  // loopback-only (audit local F5c), so we drop the fragment entirely and
  // let the on-device OpenClaw control UI handle its own auth. Off-device
  // browsers reaching the link will be blocked by nginx /gw/ deny-LAN
  // anyway (audit local F6).
  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          ...S.navItem(false),
          display: "flex", alignItems: "center", justifyContent: "space-between",
          background: "transparent", cursor: "pointer",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ fontSize: 14, lineHeight: 1 }}>⬡</span>
          Agent
        </span>
        <span style={{ fontSize: 9, color: "var(--lm-text-muted)", transition: "transform 0.15s", transform: open ? "rotate(90deg)" : "none" }}>▶</span>
      </button>
      {open && (
        <div style={{ paddingLeft: 12 }}>
          <a
            href="/gw/chat?session=agent:main:main"
            target="_blank"
            rel="noopener noreferrer"
            style={S.navItem(false)}
            onClick={closeSidebar}
            title="Opens in a new tab — Agent blocks iframe embedding"
          >
            <span style={{ fontSize: 12, lineHeight: 1 }}>↗</span>
            Gateway
          </a>
          <a
            href="#agent-config"
            style={S.navItem(section === "agent-config")}
            onClick={(e) => { e.preventDefault(); setSection("agent-config"); closeSidebar(); }}
          >
            <span style={{ fontSize: 12, lineHeight: 1 }}>◈</span>
            Config
          </a>
        </div>
      )}
    </div>
  );
}

export default function Monitor() {
  const [theme, toggleTheme, themeClass] = useTheme();
  const isDebug = new URLSearchParams(window.location.search).get("debug") === "true";
  const [section, setSectionRaw] = useState<Section>(() => {
    const h = window.location.hash.replace("#", "") as Section;
    const known = allNavLeaves().some((n) => n.id === h);
    if (!known) return "overview";
    if (!isDebug && !PUBLIC_SECTIONS.has(h)) return "overview";
    return h;
  });
  const setSection = (s: Section) => {
    window.location.hash = s;
    setSectionRaw(s);
  };

  const sectionLeaf = allNavLeaves().find((n) => n.id === section);
  const sectionLabel = sectionLeaf?.label ?? "Monitor";
  const sectionIcon = sectionLeaf?.icon ?? "";
  useDocumentTitle(sectionLabel);

  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [net, setNet] = useState<NetworkInfo | null>(null);
  const [hw, setHw] = useState<HWHealth | null>(null);
  const [oc, setOc] = useState<OCStatus | null>(null);
  const [presence, setPresence] = useState<PresenceInfo | null>(null);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [servo, setServo] = useState<ServoState | null>(null);
  const [displayState, setDisplayState] = useState<DisplayState | null>(null);
  const [audio, setAudio] = useState<AudioVolume | null>(null);
  const [musicPlaying, setMusicPlaying] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [ledColor, setLedColor] = useState<LEDColor | null>(null);
  const [sceneInfo, setSceneInfo] = useState<SceneInfo | null>(null);
  const [events, setEvents] = useState<DisplayEvent[]>([]);
  const [displayTs, setDisplayTs] = useState(0);

  const [cpuHistory, setCpuHistory] = useState<number[]>([]);
  const [ramHistory, setRamHistory] = useState<number[]>([]);
  const [lastUpdate, setLastUpdate] = useState<string>("");


  const evtIdRef = useRef(0);
  const clearFlowEvents = useCallback(() => {
    setEvents([]);
  }, []);

  // HAL version comes from /api/system/info (sys.halVersion), populated
  // by the OS server via a cached loopback call to :5001/version. Avoids a direct
  // browser fetch to /hw/version which nginx gates to loopback only.

  // One-shot fetch for system info on mount — populates sidebar version /
  // uptime labels without needing a recurring poll on every section.
  useEffect(() => {
    fetch(`${API}/system/info`).then((r) => r.json()).then((r) => {
      if (r.status === 1) setSys(r.data);
    }).catch(() => {});
  }, []);

  // Device's DECLARED capabilities, served by os-server on /api/system/info
  // (sys.capabilities) — Go owns the contract and parses DEVICE.md, so the web
  // asks the OS rather than reaching through to the HAL runtime. Used to gate
  // tabs + controls. null (not yet loaded) → show everything (fail-open).
  const caps = sys?.capabilities ? new Set(sys.capabilities) : null;
  const hasCap = (c: string): boolean => !caps || caps.has(c);
  const sectionVisible = (id: Section): boolean => {
    const cap = sectionCap(id);
    return !cap || hasCap(cap);
  };

  // If the active section is for hardware this device lacks, fall back to overview.
  useEffect(() => {
    if (caps && !sectionVisible(section)) setSection("overview");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sys?.capabilities, section]);

  // Section ref so polling callback always sees current section without re-mounting
  const sectionRef = useRef(section);
  useEffect(() => { sectionRef.current = section; }, [section]);

  // Sidebar polling: openclaw status only (needed for all tabs).
  // Runs at 10s via the shared usePolling hook, which adds a 4s hard
  // timeout, skips ticks that overlap a previous in-flight call, and
  // pauses entirely while the tab is hidden — that combination is what
  // keeps the monitor page from saturating Chrome's 6-per-origin HTTP/1.1
  // connection pool and freezing.
  usePolling(async (signal) => {
    const ocR = await fetch(`${API}/agent/status`, { signal }).then((r) => r.json());
    if (ocR.status === 1) setOc(ocR.data);
    setLastUpdate(new Date().toLocaleTimeString());
  }, 10_000);

  // Section-specific polling at 5s. The fetcher branches on the active
  // section so hidden sections don't pull data they won't show.
  //
  // Every card's fetch is fired CONCURRENTLY and commits its own state the
  // moment it resolves — no card waits on a slower sibling. Previously this was
  // three sequential `await Promise.all` waves (system → health → peripherals),
  // so the Audio panel (last wave) only appeared after system/info + network +
  // health had all returned, stacking the latency of the `/api/hardware/*`
  // proxy hops to HAL. The only real dependency is the peripheral panels on
  // `/health` (it reports which capability routes are mounted), so those alone
  // chain off it; system/info, network, presence and scene run in parallel.
  usePolling(async (signal) => {
    const s = sectionRef.current;
    const json = (r: Response) => r.json();
    const tasks: Promise<unknown>[] = [];

    if (s === "overview" || s === "system") {
      tasks.push(
        fetch(`${API}/system/info`, { signal }).then(json).then((sysR) => {
          if (sysR.status === 1) {
            const d = sysR.data;
            setSys(d);
            setCpuHistory((h) => [...h.slice(-(HISTORY_LEN - 1)), d.cpuLoad]);
            setRamHistory((h) => [...h.slice(-(HISTORY_LEN - 1)), d.memPercent]);
          }
        }).catch(() => {}),
        fetch(`${API}/system/network`, { signal }).then(json).then((netR) => {
          if (netR.status === 1) setNet(netR.data);
        }).catch(() => {}),
      );
    }

    if (s === "overview") {
      tasks.push(
        fetch(`${HW}/presence`, { signal }).then(json).then(setPresence).catch(() => {}),
        fetch(`${HW}/scene`, { signal }).then(json).then((sceneR) => {
          if (sceneR.scenes) setSceneInfo(sceneR);
        }).catch(() => {}),
        // Each peripheral panel is fetched ONLY when health reports that hardware
        // present — its HAL route is mounted by the device's declared capability,
        // so an absent peripheral means the route 404s. Gating here keeps a device
        // that lacks a peripheral (e.g. intern-v2 has no servo/display, and no
        // `media` → no music; a device with no speaker has audio:false) from
        // hitting 404 endpoints every 5s poll. Panels render null-safe when their
        // state stays at the initial value.
        fetch(`${HW}/health`, { signal }).then(json).then((hwR) => {
          setHw(hwR);
          const peripherals: Promise<unknown>[] = [];
          if (hwR.voice) peripherals.push(fetch(`${HW}/voice/status`, { signal }).then(json).then((r) => { if (r) setVoice(r); }).catch(() => {}));
          if (hwR.audio) peripherals.push(fetch(`${HW}/audio/volume`, { signal }).then(json).then((r) => { if (r) setAudio(r); }).catch(() => {}));
          if (hwR.music) peripherals.push(fetch(`${HW}/audio/status`, { signal }).then(json).then((r) => {
            if (r?.playing !== undefined) setMusicPlaying(r.playing);
            if (r?.speaker_muted !== undefined) setSpeakerMuted(r.speaker_muted);
          }).catch(() => {}));
          if (hwR.led) peripherals.push(fetch(`${HW}/led/color`, { signal }).then(json).then((r) => { if (r?.hex) setLedColor(r); }).catch(() => {}));
          if (hwR.servo) peripherals.push(fetch(`${HW}/servo`, { signal }).then(json).then((r) => { if (r) setServo(r); }).catch(() => {}));
          if (hwR.display) peripherals.push(fetch(`${HW}/display`, { signal }).then(json).then((r) => { if (r) setDisplayState(r); }).catch(() => {}));
          return Promise.all(peripherals).then(() => setDisplayTs(Date.now()));
        }).catch(() => {}),
      );
    }

    await Promise.all(tasks);
  }, 5_000, { timeoutMs: 8000 });

  // Flow SSE: only open when flow or chat section is active. useEventSource
  // auto-closes the stream on tab-hidden / unmount, freeing its connection
  // slot (one per stream against Chrome's 6-per-origin cap).
  const needsFlow = section === "flow" || section === "chat";
  useEventSource(
    needsFlow ? `${API}/agent/flow-stream` : null,
    {
      onMessage: (msg) => {
        try {
          const payload = JSON.parse(msg.data) as { events?: MonitorEvent[] };
          if (!Array.isArray(payload.events)) return;
          const next = payload.events
            .slice(-FLOW_EVENTS_MAX)
            .map((ev, i) => ({ ...ev, _seq: i }));
          setEvents(next);
          evtIdRef.current = next.length;
        } catch {}
      },
    },
  );

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const closeSidebar = () => setSidebarOpen(false);

  return (
    <div className={`lm-root ${themeClass}`} style={S.root}>
      {/* Mobile overlay */}
      <div
        className={`lm-sidebar-overlay${sidebarOpen ? " lm-sidebar-overlay--open" : ""}`}
        onClick={closeSidebar}
      />

      {/* Sidebar */}
      <aside style={S.sidebar} className={`lm-sidebar${sidebarOpen ? " lm-sidebar--open" : ""}`}>
        <nav style={{ padding: "10px 0", flex: 1 }}>
          {/* Order: Chat → Settings → Agent Gateway → System (then any other groups) */}
          {NAV.filter((e) => !isNavGroup(e) && e.id === "chat").map((entry) => {
            const leaf = entry as Extract<NavEntry, { id: Section }>;
            return (
              <a
                key={leaf.id}
                href={`#${leaf.id}`}
                style={S.navItem(section === leaf.id)}
                onClick={(e) => { e.preventDefault(); setSection(leaf.id); closeSidebar(); }}
              >
                <span style={{ fontSize: 14, lineHeight: 1 }}>{leaf.icon}</span>
                {leaf.label}
              </a>
            );
          })}
          <a href="/edit" style={S.navItem(false)} onClick={closeSidebar}>
            <span style={{ fontSize: 14, lineHeight: 1 }}>⚙</span>
            Settings
          </a>
          {isDebug && <AgentGWMenu section={section} setSection={setSection} closeSidebar={closeSidebar} />}
          {NAV
            .filter((e) => isNavGroup(e) || (!isNavGroup(e) && e.id !== "chat"))
            .map((entry) => {
              if (isNavGroup(entry)) {
                const filtered = {
                  ...entry,
                  children: entry.children.filter((c) => {
                    if (isNavLink(c)) return isDebug; // external links: debug only
                    if (!isDebug && !PUBLIC_SECTIONS.has(c.id)) return false;
                    return sectionVisible(c.id); // hide tabs for absent hardware
                  }),
                };
                if (filtered.children.length === 0) return null;
                return <NavGroupItem key={entry.group} entry={filtered} section={section} setSection={setSection} closeSidebar={closeSidebar} />;
              }
              if (!isDebug && !PUBLIC_SECTIONS.has(entry.id)) return null;
              return (
                <a
                  key={entry.id}
                  href={`#${entry.id}`}
                  style={S.navItem(section === entry.id)}
                  onClick={(e) => { e.preventDefault(); setSection(entry.id); closeSidebar(); }}
                >
                  <span style={{ fontSize: 14, lineHeight: 1 }}>{entry.icon}</span>
                  {entry.label}
                </a>
              );
            })}
        </nav>
        <div style={{
          padding: "12px 16px",
          borderTop: "1px solid var(--lm-border)",
          fontSize: 10,
          color: "var(--lm-text-muted)",
          display: "flex",
          flexDirection: "column",
          gap: 3,
        }}>
          {lastUpdate && <div>Updated {lastUpdate}</div>}
        </div>
      </aside>

      {/* Main */}
      <main style={S.main}>
        {/* Topbar: hamburger (mobile-only, left) + theme toggle (right). */}
        <div style={S.topbar}>
          <button
            className="lm-hamburger"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Menu"
          >☰</button>
          {/* Current section label — gives the user a visual anchor for where they are. */}
          <span style={{
            display: "flex", alignItems: "center", gap: 8,
            fontSize: 13, fontWeight: 600, color: "var(--lm-text)",
          }}>
            <span style={{ fontSize: 14, color: "var(--lm-amber)" }}>{sectionIcon}</span>
            <span>{sectionLabel}</span>
          </span>
          <span style={{ flex: 1 }} />
          <button onClick={toggleTheme} style={{
            background: "none", border: "1px solid var(--lm-border)", cursor: "pointer",
            fontSize: 12, color: "var(--lm-text-muted)", padding: "4px 10px",
            borderRadius: 6,
          }} title={`Theme: ${theme}`}>
            {theme === "dark" ? "◑ Dark" : "◐ Light"}
          </button>
        </div>

        {/* Content */}
        <div style={{
          ...S.content,
          ...(section === "chat" ? { padding: 0, overflow: "hidden" } : {}),
          ...(EMBED_SECTIONS.has(section) ? { padding: 0, overflow: "hidden" } : {}),
        }} className="lm-content lm-fade-in">
          {section === "overview" && (
            <OverviewSection
              sys={sys}
              net={net}
              hw={hw}
              oc={oc}
              presence={presence}
              voice={voice}
              servo={servo}
              displayState={displayState}
              audio={audio}
              musicPlaying={musicPlaying}
              speakerMuted={speakerMuted}
              ledColor={ledColor}
              sceneInfo={sceneInfo}
              hasEmotion={hasCap(Cap.Expression)}
              webVersion={__WEB_VERSION__}
              halVersion={sys?.halVersion ?? null}
              onSceneActivate={(scene) => {
                const url = scene === "off" ? `${HW}/scene/off` : `${HW}/scene`;
                const opts: RequestInit = { method: "POST", headers: { "Content-Type": "application/json" } };
                if (scene !== "off") opts.body = JSON.stringify({ scene });
                fetch(url, opts).then((r) => r.json()).then((res) => {
                  if (res.status === "ok") setSceneInfo((prev) => prev ? { ...prev, active: scene === "off" ? undefined : scene } : prev);
                }).catch(() => {});
              }}
            />
          )}
          {section === "system" && (
            <SystemSection
              sys={sys}
              net={net}
              cpuHistory={cpuHistory}
              ramHistory={ramHistory}
            />
          )}
          {section === "flow"      && <FlowSection events={events} onClearEvents={clearFlowEvents} />}
          {section === "camera"    && <CameraSection displayTs={displayTs} />}
          {section === "sensing"   && <SensingSection />}
          {section === "servo"     && <ServoSection />}
          {section === "bluetooth" && <BluetoothSection />}
          {section === "face-owners" && <FaceOwnersSection />}
          {section === "analytics" && <AnalyticsSection />}
          {section === "logs"      && <LogsSection />}
          {/* Chat is always mounted to preserve history across tab switches */}
          <div style={{ display: section === "chat" ? "contents" : "none" }}>
            <ChatSection events={events} isActive={section === "chat"} />
          </div>
          {section === "cli" && <CliSection />}
          {section === "api-docs" && (
            <iframe
              title="API Docs"
              // Routed through `/api/hardware/*` (admin-auth gated reverse
              // proxy to the device) instead of `/hw/docs` directly: nginx /hw/
              // is `allow 127.0.0.1; deny all;` per audit local F2, so the
              // direct path is broken from any remote browser. The proxy
              // accepts the session cookie via fetch credentials.
              src="/api/hardware/docs"
              style={iframeStyle}
            />
          )}
          {section === "agent-config" && (
            <iframe
              title="Agent Config"
              src="/gw-config"
              style={iframeStyle}
            />
          )}
        </div>
      </main>
    </div>
  );
}
