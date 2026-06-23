import { useEffect, useState } from "react";
import { Line } from "react-chartjs-2";
import type { TooltipItem } from "chart.js";
import { Cpu, Activity, MemoryStick, HardDrive, Thermometer, Server, Network } from "lucide-react";
import { S } from "./styles";
import type { SystemInfo, NetworkInfo } from "./types";
import { GaugeRing, StatPill, formatUptime, formatSize, CardLabel } from "./components";

// Polling interval (ms) that populates cpuHistory/ramHistory. Used to label
// the time axis on history charts since each datapoint is one poll tick.
const POLL_MS = 5000;

// Build chart.js datasets + options for a percentage history series.
// `now` is "0s" (right edge), older values stretch back as negative seconds.
// Resolve a CSS custom property to its computed color so chart.js (canvas, which
// can't read CSS vars) still tracks the active theme. Falls back to the passed
// default if the var is empty (e.g. during SSR/first paint).
function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// `colorVar` is a CSS custom property name (e.g. "--lm-amber"); resolved here so
// the line/fill colors track the theme like everything else.
function historyChart(data: number[], colorVar: string, label: string) {
  const color = cssVar(colorVar, "#f59e0b");
  // Grid/tick colors pulled from theme tokens so the chart chrome stays legible
  // on both dark and light backgrounds (the old hardcoded white vanished on light).
  const gridColor = cssVar("--lm-border", "rgba(255,255,255,0.06)");
  const tickColor = cssVar("--lm-text-muted", "rgba(255,255,255,0.4)");
  const labels = data.map((_, i) => {
    const offsetSec = (data.length - 1 - i) * (POLL_MS / 1000);
    if (offsetSec === 0) return "now";
    return `-${offsetSec >= 60 ? `${Math.round(offsetSec / 60)}m` : `${offsetSec}s`}`;
  });
  return {
    data: {
      labels,
      datasets: [{
        label,
        data,
        borderColor: color,
        backgroundColor: `${color}26`,
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 3,
        borderWidth: 1.5,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: "index" as const,
          intersect: false,
          callbacks: {
            label: (ctx: TooltipItem<"line">) => {
              const y = ctx.parsed.y;
              return y == null ? "—" : `${y.toFixed(1)}%`;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: gridColor },
          ticks: {
            color: tickColor,
            font: { size: 9 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 6,
          },
        },
        y: {
          min: 0,
          max: 100,
          grid: { color: gridColor },
          ticks: {
            color: tickColor,
            font: { size: 9 },
            stepSize: 25,
            callback: (v: string | number) => `${v}%`,
          },
        },
      },
      interaction: { mode: "nearest" as const, axis: "x" as const, intersect: false },
    },
  };
}

// Temperature tier — absolute °C, follows Pi thermal behavior.
// Standard state-tier mapping: OK / warn / crit at 60 / 75°C.
const TEMP_MAX = 80;
function tempColor(t: number): string {
  if (t > 75) return "var(--lm-red)";
  if (t > 60) return "var(--lm-amber)";
  return "var(--lm-teal)"; // identity color for Temp metric
}

// Percentage-based metric tier — same thresholds across Disk/RAM/Swap/per-core
// so a glance at any chart reads with the same convention. `identityColor` is
// what the metric shows when it's "fine" (its brand color); warnings escalate
// to amber and crits to red regardless of the metric.
function pctColor(pct: number, identityColor: string): string {
  if (pct > 85) return "var(--lm-red)";
  if (pct > 60) return "var(--lm-amber)";
  return identityColor;
}

export function SystemSection({
  sys,
  net,
  cpuHistory,
  ramHistory,
}: {
  sys: SystemInfo | null;
  net: NetworkInfo | null;
  cpuHistory: number[];
  ramHistory: number[];
}) {
  const [lastUpdate, setLastUpdate] = useState<Date>(new Date());
  useEffect(() => { if (sys) setLastUpdate(new Date()); }, [sys]);

  if (!sys) return <div style={{ color: "var(--lm-text-muted)", padding: 20 }}>Loading system data…</div>;

  const diskColor = pctColor(sys.diskPercent ?? 0, "var(--lm-teal)");

  // The `.lm-mon-card` class owns the resting + hover box-shadow (plus the
  // gradient/accent/glow), so we strip the inline boxShadow from S.card to let
  // the class's :hover shadow win — matching the Overview cards.
  const monCard = { ...S.card, boxShadow: undefined };
  const monCard12 = { ...monCard, padding: 12 };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Performance — one card per metric so each gets a clean visual unit.
          CPU card includes a compact per-core strip so spikes pinned to a single
          core (e.g. STT thread) are visible against an otherwise low aggregate. */}
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
          updated {lastUpdate.toLocaleTimeString()}
        </span>
      </div>
      {/* Row 1: CPU (1/4) + CPU history (3/4) */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 3fr", gap: 14 }}>
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<Cpu size={13} />} text="CPU" />
            <span style={{ fontSize: 11, color: "var(--lm-amber)", fontWeight: 600 }}>
              {sys.cpuCount ? `${sys.cpuCount} cores` : ""}
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
            <GaugeRing value={sys.cpuLoad} label="" detail={`${sys.cpuLoad.toFixed(1)}%`} color={pctColor(sys.cpuLoad, "var(--lm-amber)")} size={110} />
            {sys.cpuPerCore && sys.cpuPerCore.length > 0 && (
              <CoreStrip values={sys.cpuPerCore} />
            )}
          </div>
        </div>
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<Activity size={13} />} text="CPU History" />
            <span style={{ fontSize: 11, color: pctColor(sys.cpuLoad, "var(--lm-amber)"), fontWeight: 600 }}>{sys.cpuLoad.toFixed(1)}%</span>
          </div>
          <div style={{ height: 140 }}>
            {cpuHistory.length > 1 ? (
              (() => { const c = historyChart(cpuHistory, "--lm-amber", "CPU"); return <Line data={c.data} options={c.options} />; })()
            ) : <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Collecting samples…</span>}
          </div>
        </div>
      </div>

      {/* Row 2: Memory (1/4) + RAM history (3/4) */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 3fr", gap: 14 }}>
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<MemoryStick size={13} />} text="Memory" />
            <span style={{ fontSize: 11, color: "var(--lm-blue)", fontWeight: 600 }}>
              {formatSize(sys.memUsed, "KB")} / {formatSize(sys.memTotal, "KB")}
            </span>
          </div>
          {/* RAM + Swap side-by-side. Swap is smaller (size 80 vs 110) since RAM
              is the primary metric; it's hidden entirely when no swap is configured. */}
          <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12 }}>
            <GaugeRing
              value={sys.memPercent}
              label="RAM"
              detail={`${sys.memPercent.toFixed(0)}%`}
              color={pctColor(sys.memPercent, "var(--lm-blue)")}
              size={110}
            />
            {sys.swapTotal > 0 && (
              <GaugeRing
                value={sys.swapPercent}
                label="SWAP"
                detail={`${sys.swapPercent.toFixed(0)}%`}
                color={pctColor(sys.swapPercent, "var(--lm-purple)")}
                size={80}
              />
            )}
          </div>
          {sys.swapTotal > 0 && (
            <div style={{ fontSize: 10, color: "var(--lm-text-muted)", textAlign: "center", marginTop: 6, fontFamily: "monospace" }}>
              swap {formatSize(sys.swapUsed, "KB")} / {formatSize(sys.swapTotal, "KB")}
            </div>
          )}
        </div>
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<Activity size={13} />} text="RAM History" />
            <span style={{ fontSize: 11, color: pctColor(sys.memPercent, "var(--lm-blue)"), fontWeight: 600 }}>{sys.memPercent.toFixed(0)}%</span>
          </div>
          <div style={{ height: 140 }}>
            {ramHistory.length > 1 ? (
              (() => { const c = historyChart(ramHistory, "--lm-blue", "RAM"); return <Line data={c.data} options={c.options} />; })()
            ) : <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Collecting samples…</span>}
          </div>
        </div>
      </div>

      {/* Row 3: Disk + Temp + Service + Network Detail — 4 cards one row */}
      <div className="lm-grid-4">
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<HardDrive size={13} />} text="Disk" />
            <span style={{ fontSize: 11, color: diskColor, fontWeight: 600 }}>
              {formatSize(sys.diskUsed ?? 0, "MB")} / {formatSize(sys.diskTotal ?? 0, "MB")}
            </span>
          </div>
          <div style={{ display: "flex", justifyContent: "center" }}>
            <GaugeRing value={sys.diskPercent ?? 0} label="" detail={`${(sys.diskPercent ?? 0).toFixed(0)}%`} color={diskColor} size={110} />
          </div>
        </div>
        <div className="lm-mon-card" style={monCard12}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
            <CardLabel icon={<Thermometer size={13} />} text="Temp" />
            <span style={{ fontSize: 11, color: tempColor(sys.cpuTemp), fontWeight: 600 }}>{sys.cpuTemp.toFixed(1)}°C</span>
          </div>
          <div style={{ display: "flex", justifyContent: "center" }}>
            <GaugeRing
              value={sys.cpuTemp > 0 ? Math.min(100, (sys.cpuTemp / TEMP_MAX) * 100) : 0}
              label=""
              detail={`${sys.cpuTemp.toFixed(1)}°C`}
              color={tempColor(sys.cpuTemp)}
              size={110}
            />
          </div>
        </div>
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Server size={13} />} text="Service" /></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <StatPill label="OS Uptime"       value={formatUptime(sys.uptime)}                                                  bullet="var(--lm-text-dim)" />
            <StatPill label="Server Uptime"   value={sys.serviceUptime ? formatUptime(sys.serviceUptime) : "—"} color="var(--lm-amber)" bullet="var(--lm-amber)" />
            <StatPill label="Go Routines"     value={sys.goRoutines}                                            color="var(--lm-amber)" bullet="var(--lm-amber)" />
            <StatPill label="Hardware Uptime" value={sys.halUptime ? formatUptime(sys.halUptime) : "—"}   color="var(--lm-blue)"  bullet="var(--lm-blue)" />
            <DeviceIdPill deviceId={sys.deviceId} />
          </div>
        </div>
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Network size={13} />} text="Network Detail" /></div>
          {net ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatPill label="Link Rate"    value={net.linkRate > 0 ? `${net.linkRate} Mbps` : "—"} color="var(--lm-teal)" />
              <StatPill label="Signal"       value={net.signal !== 0 ? `${net.signal} dBm` : "—"} />
              <StatPill label="Public IP"    value={net.publicIp || "—"} color="var(--lm-amber)" />
              <StatPill label="Tailscale IP" value={net.tailscaleIp || "—"} color={net.tailscaleIp ? "var(--lm-teal)" : undefined} />
              <StatPill label="MAC"          value={net.mac || "—"} />
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)" }}>No network data</span>}
        </div>
      </div>
    </div>
  );
}

// CoreStrip renders per-core load as small vertical bars side by side —
// the compact "CPU history" look from system monitors. Hover for exact %.
// Uses pure state-tier colors (green/amber/red) since the strip's whole
// purpose is to surface which core is hot — identity-amber for all cores
// would defeat the visual signal.
function CoreStrip({ values }: { values: number[] }) {
  const coreColor = (p: number) =>
    p > 85 ? "var(--lm-red)" : p > 60 ? "var(--lm-amber)" : "var(--lm-green)";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4, width: "100%" }}>
      <div style={{
        display: "flex",
        gap: 3,
        alignItems: "flex-end",
        height: 22,
        padding: "0 4px",
      }}>
        {values.map((p, i) => {
          const clamped = Math.max(0, Math.min(100, p));
          const c = coreColor(clamped);
          return (
            <div
              key={i}
              title={`Core ${i}: ${clamped.toFixed(0)}%`}
              style={{
                width: 7,
                height: "100%",
                background: "var(--lm-surface)",
                borderRadius: 2,
                position: "relative",
                overflow: "hidden",
              }}
            >
              <div style={{
                position: "absolute",
                left: 0,
                right: 0,
                bottom: 0,
                height: `${Math.max(4, clamped)}%`,
                background: c,
                transition: "height 0.6s ease, background 0.3s ease",
              }} />
            </div>
          );
        })}
      </div>
      <span style={{ fontSize: 9, color: "var(--lm-text-muted)", letterSpacing: 0.3 }}>per-core</span>
    </div>
  );
}

// DeviceIdPill shows the full ID truncated, with click-to-copy.
function DeviceIdPill({ deviceId }: { deviceId: string }) {
  const [copied, setCopied] = useState(false);
  if (!deviceId) {
    return <StatPill label="Device ID" value="—" />;
  }
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(deviceId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  };
  return (
    <div
      onClick={onCopy}
      title="Click to copy"
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: 10,
        padding: "6px 12px",
        background: "var(--lm-surface)",
        borderRadius: 8,
        border: "1px solid var(--lm-border)",
        cursor: "pointer",
      }}
    >
      <span style={{ fontSize: 11.5, color: "var(--lm-text-dim)", flexShrink: 0 }}>Device ID</span>
      <span style={{
        fontSize: 11,
        fontWeight: 600,
        fontFamily: "monospace",
        color: copied ? "var(--lm-green)" : "var(--lm-text)",
        wordBreak: "break-all",
        textAlign: "right",
      }}>
        {copied ? "copied!" : deviceId}
      </span>
    </div>
  );
}
