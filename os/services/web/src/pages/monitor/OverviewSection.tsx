import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Satellite, Globe, Eye, Volume2, Cpu, Drama, Clapperboard, Bot, Tag, Wifi, LayoutDashboard } from "lucide-react";
import { S } from "./styles";
import { API, HW } from "./types";

const EMOTION_EMOJI: Record<string, string> = {
  happy: "😊", curious: "🤔", thinking: "💭", sad: "😢", excited: "🤩",
  shy: "😳", shock: "😱", idle: "😐", listening: "👂", laugh: "😄",
  confused: "😕", sleepy: "😴", greeting: "👋", goodbye: "👋", acknowledge: "👍",
  stretching: "🙆", caring: "🤗", music_chill: "🎵", music_strong: "🎸",
  scan: "👀", nod: "👍", headshake: "🙅",
};

function rgbToHex(rgb: number[]): string {
  return "#" + rgb.map(c => c.toString(16).padStart(2, "0")).join("");
}

function useEmotionPresets() {
  const [emotions, setEmotions] = useState<string[]>([]);
  const [colors, setColors] = useState<Record<string, string>>({});
  useEffect(() => {
    fetch(`${HW}/emotion/presets`)
      .then(r => r.json())
      .then((data: Record<string, { color: number[]; effect: string; speed: number }>) => {
        setEmotions(Object.keys(data));
        const c: Record<string, string> = {};
        for (const [name, preset] of Object.entries(data)) {
          c[name] = rgbToHex(preset.color);
        }
        setColors(c);
      })
      .catch(() => {});
  }, []);
  return { emotions, colors };
}
import type { SystemInfo, NetworkInfo, HWHealth, OCStatus, PresenceInfo, VoiceStatus, ServoState, DisplayState, AudioVolume, LEDColor, SceneInfo } from "./types";
import { StatusDot, HWBadge, SignalBars, formatUptime, formatAgo, useCountUp, Skeleton, SkeletonRows, SoftwareUpdateButton, StatRow, StatusBadge, STATUS_TONE, CardLabel } from "./components";
import { BuddyCard } from "./BuddyCard";

export function OverviewSection({
  sys,
  net,
  hw,
  oc,
  presence,
  voice,
  servo,
  displayState,
  audio,
  musicPlaying,
  speakerMuted,
  ledColor,
  sceneInfo,
  hasEmotion,
  hasMotion,
  webVersion,
  halVersion,
  onSceneActivate,
}: {
  sys: SystemInfo | null;
  net: NetworkInfo | null;
  hw: HWHealth | null;
  oc: OCStatus | null;
  presence: PresenceInfo | null;
  voice: VoiceStatus | null;
  servo: ServoState | null;
  displayState: DisplayState | null;
  audio: AudioVolume | null;
  musicPlaying: boolean;
  speakerMuted: boolean;
  ledColor: LEDColor | null;
  sceneInfo: SceneInfo | null;
  hasEmotion: boolean; // device declares the expression capability (/emotion route)
  hasMotion: boolean; // device declares the motion capability (/servo route)
  webVersion: string;
  halVersion: string | null;
  onSceneActivate: (scene: string) => void;
}) {
  const { emotions: ALL_EMOTIONS, colors: EMOTION_COLOR } = useEmotionPresets();
  const emotion = oc?.emotion ?? "";
  const emotionColor = EMOTION_COLOR[emotion] ?? "var(--lm-text-muted)";
  const emotionEmoji = EMOTION_EMOJI[emotion] ?? "✦";

  // Software-update buttons in the Versions card are gated behind ?debug=true
  // so the regular monitor view doesn't ship one-click OTA triggers (rate
  // limit + admin auth still apply on the server side either way).
  const isDebug = new URLSearchParams(window.location.search).get("debug") === "true";

  // Volume slider: local state for smooth dragging, API call only on release
  const [localVolume, setLocalVolume] = useState<number | null>(null);
  const draggingVolume = useRef(false);
  // Reactive mirror of draggingVolume so render can decide between the exact
  // handle value (mid-drag) and the eased count-up (idle) without reading a ref.
  const [dragging, setDragging] = useState(false);

  // Sync from server when not dragging
  useEffect(() => {
    if (!draggingVolume.current && audio?.volume != null) {
      setLocalVolume(audio.volume);
    }
  }, [audio?.volume]);

  // Animated stats: link rate and volume tick to their new value instead of
  // snapping, so a refresh reads as a live needle rather than a hard cut.
  const animatedLinkRate = useCountUp(net?.linkRate ?? 0);
  const animatedVolume = useCountUp(localVolume ?? audio?.volume ?? 0);

  const commitVolume = useCallback((vol: number) => {
    draggingVolume.current = false;
    setDragging(false);
    fetch(`${HW}/audio/volume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }),
    }).catch(() => {});
  }, []);

  // Base card style for the Overview: the `.lm-mon-card` class owns the
  // resting + hover box-shadow (and the gradient/accent/glow), so we strip the
  // inline boxShadow from S.card to let the class's :hover shadow win.
  const monCard = { ...S.card, boxShadow: undefined };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

      {/* Hero banner — a command-center header summarizing live device state.
          Read-only: every chip reflects an already-fetched prop, no new calls. */}
      <div className="lm-mon-hero">
        <div style={{ position: "relative", zIndex: 1, display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div style={{
              width: 44, height: 44, borderRadius: 12, flexShrink: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
              background: "var(--lm-amber-dim)", color: "var(--lm-amber)",
              boxShadow: "inset 0 0 0 1px var(--lm-amber-glow)",
            }} aria-hidden><LayoutDashboard size={22} /></div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 19, fontWeight: 700, color: "var(--lm-text)", letterSpacing: "-0.3px", lineHeight: 1.2 }}>
                Device Overview
              </div>
              <div style={{ fontSize: 12, color: "var(--lm-text-dim)", marginTop: 2 }}>
                Live status across agent, network, presence & hardware
              </div>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <HeroChip
              icon={<Bot size={14} />}
              label="Agent"
              value={oc?.connected ? "Online" : "Offline"}
              tone={oc?.connected ? "ok" : "error"}
            />
            <HeroChip icon={<Wifi size={14} />} label="IP" value={net?.ip ?? "—"} tone="neutral" />
            <HeroChip
              icon={<Eye size={14} />}
              label="Presence"
              value={presence?.state ? presence.state[0].toUpperCase() + presence.state.slice(1) : "—"}
              tone={presence?.state === "active" ? "active" : "neutral"}
            />
          </div>
        </div>
      </div>

      {/* Row 1: 4 status cards in one row */}
      <div className="lm-grid-4">
        {/* Agent Gateway */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Satellite size={13} />} text="Agent Gateway" />
            <StatusBadge text={oc?.connected ? "ONLINE" : "OFFLINE"} ok={!!oc?.connected} pulse={!!oc?.connected} />
          </div>
          {oc ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="Agent" value={oc.name} />
              {oc.version && <StatRow label="Version" value={oc.version} mono />}
              <StatRow label="Session" value={
                <span style={{
                  fontSize: 10, padding: "1px 6px", borderRadius: 4, fontWeight: 600,
                  background: oc.sessionKey ? "rgba(52,211,153,0.1)" : "rgba(80,74,60,0.4)",
                  color: oc.sessionKey ? "var(--lm-green)" : "var(--lm-text-muted)",
                }}>
                  {oc.sessionKey ? "Active" : "Pending"}
                </span>
              } />
              {oc.emotion && <StatRow label="Emotion" value={oc.emotion} color="var(--lm-amber)" />}
            </div>
          ) : <SkeletonRows lines={3} />}
        </div>

        {/* Network */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Globe size={13} />} text="Network" />
          </div>
          {net ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="SSID" value={net.ssid || "—"} />
              <StatRow label="IP" value={net.ip} color="var(--lm-teal)" />
              {net.tailscaleIp && <StatRow label="Tailscale" value={net.tailscaleIp} color="var(--lm-teal)" />}
              <StatRow label="Internet" value={net.internet ? "Connected" : "No"} color={net.internet ? "var(--lm-green)" : "var(--lm-red)"} />
              <StatRow label="Speed" value={
                <span style={{ display: "flex", alignItems: "center", gap: 6 }} title={`Signal ${net.signal} dBm`}>
                  <SignalBars value={net.signal} />
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>
                    {net.linkRate > 0 ? `${animatedLinkRate} Mbps` : "—"}
                  </span>
                </span>
              } />
              <StatRow label="MAC" value={net.mac || "—"} mono />
            </div>
          ) : <SkeletonRows lines={5} />}
        </div>

        {/* Presence */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Eye size={13} />} text="Presence" />
            <StatusBadge text={(presence?.state ?? "—").toUpperCase()} tone={presence?.state === "active" ? "active" : "idle"} pulse={presence?.state === "active"} />
          </div>
          {presence ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="Sensing" value={presence.enabled ? "On" : "Off"} color={presence.enabled ? "var(--lm-green)" : "var(--lm-red)"} />
              <StatRow label="Last motion" value={formatAgo(presence.seconds_since_motion)} />
            </div>
          ) : <SkeletonRows lines={2} />}
        </div>

        {/* Audio */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Volume2 size={13} />} text="Audio" /></div>
          {voice ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {/* Mic row */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={voice.voice_available && !voice.mic_muted} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>Mic</span>
                  {voice.mic_muted ? (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(239,68,68,0.12)", color: "#f87171" }}>MUTED</span>
                  ) : voice.voice_listening ? (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "var(--lm-amber-dim)", color: "var(--lm-amber)" }}>LIVE</span>
                  ) : null}
                </div>
                <ToggleButton active={!voice.mic_muted} label={voice.mic_muted ? "Unmute" : "Mute"}
                  onClick={() => fetch(`${HW}/voice/${voice.mic_muted ? "unmute" : "mute"}`, { method: "POST" }).catch(() => {})} />
              </div>

              {/* TTS row */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={voice.tts_available} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>TTS</span>
                  {voice.tts_speaking && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(167,139,250,0.15)", color: "var(--lm-purple)" }}>SPEAKING</span>
                  )}
                  {musicPlaying && !voice.tts_speaking && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(52,211,153,0.12)", color: "var(--lm-green)" }}>MUSIC</span>
                  )}
                </div>
                {(voice.tts_speaking || musicPlaying) && (
                  <ToggleButton active={false} label="Stop"
                    onClick={() => fetch(`${API}/agent/tts/stop`, { method: "POST" }).catch(() => {})} />
                )}
              </div>

              {/* Speaker row */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={!speakerMuted} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>Speaker</span>
                  {speakerMuted && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(239,68,68,0.12)", color: "#f87171" }}>MUTED</span>
                  )}
                </div>
                <ToggleButton active={!speakerMuted} label={speakerMuted ? "Unmute" : "Mute"}
                  onClick={() => fetch(`${HW}/speaker/${speakerMuted ? "unmute" : "mute"}`, { method: "POST" }).catch(() => {})} />
              </div>

              {/* Volume slider */}
              <div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Volume</span>
                  <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
                    {/* While dragging show the exact handle value (no easing lag);
                        when idle let it tick to the server-confirmed value. */}
                    {dragging ? (localVolume ?? audio?.volume ?? "—") : animatedVolume}%
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={localVolume ?? audio?.volume ?? 50}
                  onChange={(e) => {
                    draggingVolume.current = true;
                    setDragging(true);
                    setLocalVolume(Number(e.target.value));
                  }}
                  onMouseUp={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  onTouchEnd={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  className="lm-mon-range"
                  style={{
                    width: "100%", cursor: "pointer",
                    // Drives the amber-fill width in the .lm-mon-range track (paint only).
                    ["--lm-fill" as string]: `${localVolume ?? audio?.volume ?? 50}%`,
                  }}
                />
              </div>
            </div>
          ) : <AudioSkeleton />}
        </div>
      </div>

      {/* Row 2: device & capability cluster, split into two masonry columns so
          cards pack by their natural height instead of being forced to equal
          height by one auto-fit grid (which left dead space in the compact cards
          and cramped the tall pill-cloud ones). The RIGHT column carries the
          expressive cards (Emotion + Servo pose), each with a scrollable pill
          cloud; the LEFT column carries the compact status cards (Hardware,
          Scene, Versions, Buddy). DOM order keeps the expressive cards first but
          CSS `order` paints them on the right. On a minimal device the capability-gated cards
          simply don't render, and the remaining cards still read cleanly. On a
          narrow viewport the two columns collapse to one (see .lm-cluster). */}
      <div className="lm-cluster">
        {/* Expressive cards (Emotion + Servo) — painted on the RIGHT via order: 2 */}
        <div className="lm-cluster-col" style={{ order: 2 }}>
        {/* Emotion — only for devices that declare the expression capability (the
            /emotion route). intern-v2 has no expression, so the whole card is hidden
            rather than showing an agent emotion the device can't actually express. */}
        {hasEmotion && (
        <div style={{
          ...S.card, padding: "14px 16px",
          background: emotion ? `linear-gradient(135deg, var(--lm-bg) 60%, ${emotionColor}18)` : "var(--lm-bg)",
          border: `1px solid ${emotion ? emotionColor + "55" : "var(--lm-border)"}`,
          transition: "all 0.4s ease",
        }}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Drama size={13} />} text="Emotion" /></div>
          {/* Two-column body: the current-emotion summary (emoji + name) sits in
              a fixed-width left column, and the full preset list wraps as a tidy
              cloud filling the rest on the right — instead of stacking the cloud
              full-width under the summary. Wraps to stacked under ~360px. */}
          <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "flex-start", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flex: "0 0 140px", minWidth: 0 }}>
              <div style={{
                fontSize: 36, lineHeight: 1, flexShrink: 0,
                filter: emotion ? `drop-shadow(0 0 8px ${emotionColor}88)` : "none",
                transition: "filter 0.4s ease",
              }}>
                {emotion ? emotionEmoji : "✦"}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 2, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Your device is feeling
                </div>
                <div style={{ fontSize: 18, fontWeight: 700, color: emotion ? emotionColor : "var(--lm-text-muted)", textTransform: "capitalize", transition: "color 0.4s ease" }}>
                  {emotion || "—"}
                </div>
              </div>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <PillCloud
                items={ALL_EMOTIONS}
                active={emotion}
                label={(e) => <>{EMOTION_EMOJI[e]} {e}</>}
                accent={(e) => EMOTION_COLOR[e] ?? "#fff"}
                onPick={(e) => {
                  fetch(`${HW}/emotion`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ emotion: e, intensity: 1.0 }),
                  }).catch(() => {});
                }}
                title={(e) => `Test emotion: ${e}`}
              />
            </div>
          </div>
        </div>
        )}

        {/* Servo — only for devices that declare the motion capability (e.g. lamp, not intern-v2) */}
        {hasMotion && (
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Bot size={13} />} text="Servo Pose" /></div>
          {servo ? (
            // Two-column body mirroring the Emotion card: the current pose +
            // Release control sit in a fixed-width left column, and the recording
            // list wraps as a tidy cloud filling the rest on the right. Wraps to
            // stacked under ~360px.
            <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "flex-start", gap: 16 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: "0 0 140px", minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-amber)" }}>
                  {servo.current || "idle"}
                  {(servo.bus_connected === false || servo.robot_connected === false) && (
                    <span style={{ fontSize: 10, color: "var(--lm-danger, #c44)", marginLeft: 6 }}>
                      (bus {servo.bus_connected === false ? "down" : "ok"}{servo.robot_connected === false ? ", robot off" : ""})
                    </span>
                  )}
                </div>
                <button className="lm-u-btn" onClick={() => {
                  fetch(`${HW}/servo/release`, { method: "POST", headers: { accept: "application/json" } }).catch(() => {});
                }} style={{
                  fontSize: 10, padding: "3px 9px", borderRadius: 6,
                  color: "var(--lm-text-dim)", alignSelf: "flex-start",
                }}>Release</button>
              </div>
              <div style={{ flex: "1 1 200px", minWidth: 0 }}>
                <PillCloud
                  items={servo.available_recordings ?? []}
                  active={servo.current ?? ""}
                  label={(p) => p}
                  accent={() => "var(--lm-amber)"}
                  onPick={(p) => {
                    fetch(`${HW}/servo/play`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ recording: p }),
                    }).catch(() => {});
                  }}
                />
              </div>
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>
        )}

        {/* Versions — kept in the expressive (right) column so the two columns
            balance in height: Hardware + Scene + Buddy on the left roughly match
            Emotion + Servo + Versions on the right, instead of the right column
            ending short under Servo Pose. OS uptime sits in the host row;
            detailed CPU/RAM/Disk live in the System tab. */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 10 }}><CardLabel icon={<Tag size={13} />} text="Versions" /></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <VersionRow name="Host"   color="var(--lm-text)"   version={null}                    uptime={sys?.uptime ?? null}                                   updateTarget={null} />
            <VersionRow name="Web"    color="var(--lm-teal)"   version={webVersion}              uptime={null}                                                  updateTarget={isDebug ? "web" : null} />
            <VersionRow name="OS"     color="var(--lm-amber)"  version={sys?.version ?? null}    uptime={sys?.serviceUptime ?? null}                            updateTarget={isDebug ? "os-server" : null} />
            <VersionRow name="HAL"    color="var(--lm-blue)"   version={halVersion}              uptime={sys?.halUptime ?? null}                                updateTarget={isDebug ? "hal" : null} />
            <VersionRow name="Agent"  color="var(--lm-purple)" version={oc?.version ?? null}     uptime={oc?.connected ? (oc?.agentUptime ?? null) : null}      updateTarget={null} />
          </div>
        </div>
        </div>

        {/* Compact status cards — painted on the LEFT via order: 1 */}
        <div className="lm-cluster-col" style={{ order: 1 }}>
        {/* Hardware */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Cpu size={13} />} text="Hardware" />
            {ledColor && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{
                  width: 14, height: 14, borderRadius: "50%",
                  background: ledColor.on ? ledColor.hex : "transparent",
                  boxShadow: ledColor.on ? `0 0 8px ${ledColor.hex}cc` : "none",
                  border: `2px solid ${ledColor.on ? ledColor.hex : "var(--lm-border)"}`,
                  flexShrink: 0,
                }} title={`RGB(${ledColor.color.join(", ")})`} />
                <span style={{ fontSize: 10, fontFamily: "monospace", color: ledColor.on ? "var(--lm-text)" : "var(--lm-text-muted)" }}>
                  {ledColor.on ? ledColor.hex : "off"}
                </span>
                {ledColor.on && (
                  <span style={{ fontSize: 10, color: "var(--lm-text-dim)" }}>
                    {Math.round(ledColor.brightness * 100)}%
                  </span>
                )}
                {ledColor.effect && (
                  <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 4, background: "rgba(167,139,250,0.15)", color: "var(--lm-purple)", fontWeight: 600 }}>
                    {ledColor.effect}
                  </span>
                )}
                {ledColor.scene && !ledColor.effect && (
                  <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 4, background: "var(--lm-amber-dim)", color: "var(--lm-amber)", fontWeight: 600 }}>
                    {ledColor.scene}
                  </span>
                )}
              </div>
            )}
          </div>
          {hw ? (
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 7 }}>
              <HWBadge label="Servo" ok={hw.servo} />
              <HWBadge label="LED" ok={hw.led} />
              <HWBadge label="Camera" ok={hw.camera} />
              <HWBadge label="Audio" ok={hw.audio} />
              <HWBadge label="Sensing" ok={hw.sensing} />
              <HWBadge label="Voice" ok={hw.voice} />
              <HWBadge label="TTS" ok={hw.tts} />
            </div>
          ) : <SkeletonRows lines={2} />}
        </div>

        {/* Scene — `scene` is a route WITHIN the `light` capability (lamp declares
            light:[led,scene]; intern-v2 declares light:[led] only), so the capability
            list can't distinguish it. Gate on data instead: a device whose light
            capability omits the scene route leaves /scene 404ing, so sceneInfo stays
            null and the card never renders (rather than showing "Loading…" forever). */}
        {sceneInfo && (
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Clapperboard size={13} />} text="Scene" /></div>
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 5 }}>
              {sceneInfo.scenes.map((s) => (
                <span key={s} role="button" onClick={() => onSceneActivate(s)} style={{
                  fontSize: 11,
                  padding: "3px 9px",
                  borderRadius: 6,
                  background: s === sceneInfo.active ? "var(--lm-amber-dim)" : "var(--lm-surface)",
                  border: `1px solid ${s === sceneInfo.active ? "var(--lm-amber)" : "var(--lm-border)"}`,
                  color: s === sceneInfo.active ? "var(--lm-amber)" : "var(--lm-text-dim)",
                  cursor: "pointer",
                  fontWeight: s === sceneInfo.active ? 600 : 400,
                  textTransform: "capitalize",
                }}>{s}</span>
              ))}
              <span role="button" onClick={() => onSceneActivate("off")} style={{
                fontSize: 11,
                padding: "3px 9px",
                borderRadius: 6,
                background: !sceneInfo.active ? "var(--lm-red)" : "var(--lm-surface)",
                border: `1px solid ${!sceneInfo.active ? "var(--lm-red)" : "var(--lm-border)"}`,
                color: !sceneInfo.active ? "#fff" : "var(--lm-text-dim)",
                cursor: "pointer",
                fontWeight: !sceneInfo.active ? 600 : 400,
              }}>Off</span>
            </div>
        </div>
        )}

        {/* Autonomous Buddy pairing — closes out the compact (left) column. */}
        <BuddyCard />
        </div>
      </div>

      {/* Display Eyes — hidden via display:none, code kept for future re-enable */}
      <div style={{ ...S.card, display: "none" }}>
        <div style={S.cardLabel}>Display Eyes</div>
        {displayState ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <StatusDot ok={displayState.hardware} />
              <span style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-teal)" }}>{displayState.mode}</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4 }}>
              {(displayState.available_expressions ?? []).map((e) => (
                <span key={e} style={{
                  fontSize: 10, padding: "2px 6px", borderRadius: 4,
                  background: e === displayState.mode ? "rgba(45,212,191,0.12)" : "var(--lm-surface)",
                  border: `1px solid ${e === displayState.mode ? "rgba(45,212,191,0.4)" : "var(--lm-border)"}`,
                  color: e === displayState.mode ? "var(--lm-teal)" : "var(--lm-text-dim)",
                }}>{e}</span>
              ))}
            </div>
          </div>
        ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
      </div>

    </div>
  );
}

// PillCloud renders a de-cluttered, free-wrapping grid of selectable pills for
// the Emotion / Servo cards. Both previously dumped every preset (20+ emotions,
// 30+ poses) as tiny tags that buried the card and read as noise. Here the active
// pill is hoisted to the front so the current state always reads first, and the
// whole set wraps as a tidy cloud (no scroll). Pills keep their per-item accent
// color.
function PillCloud<T extends string>({ items, active, label, accent, onPick, title }: {
  items: T[];
  active: string;
  label: (item: T) => ReactNode;
  accent: (item: T) => string;
  onPick: (item: T) => void;
  title?: (item: T) => string;
}) {
  // Active item first so the current state always reads first.
  const ordered = active && items.includes(active as T)
    ? [active as T, ...items.filter((i) => i !== active)]
    : items;
  return (
    <div className="lm-pillcloud">
      {ordered.map((item) => {
        const isActive = item === active;
        const c = accent(item);
        return (
          <span
            key={item}
            role="button"
            title={title?.(item)}
            onClick={() => onPick(item)}
            style={{
              fontSize: 10, padding: "2px 8px", borderRadius: 999,
              background: isActive ? `${c}22` : "var(--lm-surface)",
              border: `1px solid ${isActive ? c + "88" : "var(--lm-border)"}`,
              color: isActive ? c : "var(--lm-text-muted)",
              fontWeight: isActive ? 700 : 400,
              textTransform: "capitalize",
              transition: "all 0.2s ease",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {label(item)}
          </span>
        );
      })}
    </div>
  );
}

// HeroChip is a compact pill in the hero banner showing a single live stat
// (Agent / IP / Presence). Presentational only — values are passed in.
function HeroChip({ icon, label, value, tone }: {
  icon: ReactNode;
  label: string;
  value: string;
  tone: "ok" | "error" | "active" | "neutral";
}) {
  const color =
    tone === "ok" ? "var(--lm-green)" :
    tone === "error" ? "var(--lm-red)" :
    tone === "active" ? "var(--lm-amber)" :
    "var(--lm-text)";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "6px 12px", borderRadius: 10,
      background: "color-mix(in srgb, var(--lm-card) 70%, transparent)",
      border: "1px solid var(--lm-border)",
      backdropFilter: "blur(4px)",
    }}>
      <span style={{ display: "flex", color }} aria-hidden>{icon}</span>
      <span style={{ fontSize: 10, color: "var(--lm-text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      <span style={{ fontSize: 12.5, fontWeight: 700, color, fontFamily: label === "IP" ? "monospace" : undefined }}>{value}</span>
    </div>
  );
}

// AudioSkeleton mirrors the Audio card's layout (three labelled toggle rows +
// a volume slider) so the card holds its height while voice status loads,
// avoiding the jump the old "Loading…" text caused.
function AudioSkeleton() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {[0, 1, 2].map((i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Skeleton width={7} height={7} style={{ borderRadius: "50%" }} />
            <Skeleton width={54} height={12} />
          </div>
          <Skeleton width={60} height={24} style={{ borderRadius: 6 }} />
        </div>
      ))}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Skeleton width="40%" height={11} />
        <Skeleton width="100%" height={6} style={{ borderRadius: 999 }} />
      </div>
    </div>
  );
}

// ToggleButton is the small Mute/Unmute/Stop control in the Audio card. When
// `active` (e.g. mic live) it shows a destructive red tone; when inactive
// (already muted) it offers a green "Unmute". Tones come from STATUS_TONE so
// they stay theme-aware (the old code hardcoded #f87171 which broke on light).
function ToggleButton({ active, label, onClick }: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  const tone = active ? STATUS_TONE.error : STATUS_TONE.ok;
  return (
    <button className="lm-u-btn" onClick={onClick} style={{
      fontSize: 11, padding: "5px 14px", borderRadius: 6, fontWeight: 600,
      background: tone.bg, border: `1px solid ${tone.border}`, color: tone.color,
    }}>
      {label}
    </button>
  );
}

function VersionRow({ name, color, version, uptime, updateTarget }: {
  name: string;
  color: string;
  version: string | null;
  uptime: number | null;
  updateTarget: "os-server" | "web" | "hal" | null;
}) {
  // 4-column grid keeps name/version/uptime/button vertically aligned across rows.
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "70px 1fr 70px 70px",
      alignItems: "center",
      gap: 10,
    }}>
      <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>{name}</span>
      <span style={{ fontSize: 12.5, fontWeight: 600, color, fontFamily: "monospace" }}>{version ?? "—"}</span>
      <span style={{ fontSize: 11, color: "var(--lm-text-muted)", textAlign: "right" }}>
        {uptime != null ? formatUptime(uptime) : "—"}
      </span>
      <span style={{ display: "flex", justifyContent: "flex-end" }}>
        {updateTarget && <SoftwareUpdateButton target={updateTarget} label="update" />}
      </span>
    </div>
  );
}
