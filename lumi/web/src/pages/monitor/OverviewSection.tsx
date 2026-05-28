import { useCallback, useEffect, useRef, useState } from "react";
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
import { StatusDot, HWBadge, SignalBars, formatUptime, SoftwareUpdateButton } from "./components";
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
  webVersion,
  lelampVersion,
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
  webVersion: string;
  lelampVersion: string | null;
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

  // Sync from server when not dragging
  useEffect(() => {
    if (!draggingVolume.current && audio?.volume != null) {
      setLocalVolume(audio.volume);
    }
  }, [audio?.volume]);

  const commitVolume = useCallback((vol: number) => {
    draggingVolume.current = false;
    fetch(`${HW}/audio/volume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }),
    }).catch(() => {});
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

      {/* Row 1: 4 status cards in one row */}
      <div className="lm-grid-4">
        {/* Agent Gateway */}
        <div style={S.card}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <div style={S.cardLabel}>Agent Gateway</div>
            <span style={{
              fontSize: 10, padding: "3px 9px", borderRadius: 4, fontWeight: 700,
              background: oc?.connected ? "rgba(52,211,153,0.1)" : "rgba(239,68,68,0.1)",
              color: oc?.connected ? "var(--lm-green)" : "var(--lm-red)",
              border: `1px solid ${oc?.connected ? "rgba(52,211,153,0.3)" : "rgba(239,68,68,0.3)"}`,
            }}>
              {oc?.connected ? "ONLINE" : "OFFLINE"}
            </span>
          </div>
          {oc ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Agent</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>{oc.name}</span>
              </div>
              {oc.version && (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Version</span>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)", fontFamily: "monospace" }}>{oc.version}</span>
                </div>
              )}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Session</span>
                <span style={{
                  fontSize: 10, padding: "1px 6px", borderRadius: 4, fontWeight: 600,
                  background: oc.sessionKey ? "rgba(52,211,153,0.1)" : "rgba(80,74,60,0.4)",
                  color: oc.sessionKey ? "var(--lm-green)" : "var(--lm-text-muted)",
                }}>
                  {oc.sessionKey ? "Active" : "Pending"}
                </span>
              </div>
              {oc.emotion && (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Emotion</span>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-amber)" }}>{oc.emotion}</span>
                </div>
              )}
            </div>
          ) : <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>

        {/* Network */}
        <div style={S.card}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <div style={S.cardLabel}>Network</div>
          </div>
          {net ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>SSID</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>{net.ssid || "—"}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>IP</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-teal)" }}>{net.ip}</span>
              </div>
              {net.tailscaleIp && (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Tailscale</span>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-teal)" }}>{net.tailscaleIp}</span>
                </div>
              )}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Internet</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: net.internet ? "var(--lm-green)" : "var(--lm-red)" }}>
                  {net.internet ? "Connected" : "No"}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Speed</span>
                <span style={{ display: "flex", alignItems: "center", gap: 6 }} title={`Signal ${net.signal} dBm`}>
                  <SignalBars value={net.signal} />
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>
                    {net.linkRate > 0 ? `${net.linkRate} Mbps` : "—"}
                  </span>
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>MAC</span>
                <span style={{ fontSize: 11, fontFamily: "monospace", color: "var(--lm-text)" }}>{net.mac || "—"}</span>
              </div>
            </div>
          ) : <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>

        {/* Presence */}
        <div style={S.card}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <div style={S.cardLabel}>Presence</div>
            <span style={{
              fontSize: 10, padding: "3px 9px", borderRadius: 4, fontWeight: 700,
              background: presence?.state === "active" ? "rgba(245,158,11,0.1)" : "rgba(80,74,60,0.4)",
              color: presence?.state === "active" ? "var(--lm-amber)" : "var(--lm-text-muted)",
              border: `1px solid ${presence?.state === "active" ? "rgba(245,158,11,0.3)" : "var(--lm-border)"}`,
            }}>
              {(presence?.state ?? "—").toUpperCase()}
            </span>
          </div>
          {presence ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Sensing</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: presence.enabled ? "var(--lm-green)" : "var(--lm-red)" }}>
                  {presence.enabled ? "On" : "Off"}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>Last motion</span>
                <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>{presence.seconds_since_motion}s ago</span>
              </div>
            </div>
          ) : <span style={{ fontSize: 11, color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>

        {/* Audio */}
        <div style={S.card}>
          <div style={S.cardLabel}>Audio</div>
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
                <button onClick={() => fetch(`${HW}/voice/${voice.mic_muted ? "unmute" : "mute"}`, { method: "POST" }).catch(() => {})} style={{
                  fontSize: 11, padding: "5px 14px", borderRadius: 6, fontWeight: 600, cursor: "pointer",
                  background: voice.mic_muted ? "rgba(52,211,153,0.1)" : "rgba(239,68,68,0.08)",
                  border: `1px solid ${voice.mic_muted ? "rgba(52,211,153,0.3)" : "rgba(239,68,68,0.25)"}`,
                  color: voice.mic_muted ? "var(--lm-green)" : "#f87171",
                }}>
                  {voice.mic_muted ? "Unmute" : "Mute"}
                </button>
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
                  <button onClick={() => fetch(`${API}/agent/tts/stop`, { method: "POST" }).catch(() => {})} style={{
                    fontSize: 11, padding: "5px 14px", borderRadius: 6, fontWeight: 600, cursor: "pointer",
                    background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.25)", color: "#f87171",
                  }}>Stop</button>
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
                <button onClick={() => fetch(`${HW}/speaker/${speakerMuted ? "unmute" : "mute"}`, { method: "POST" }).catch(() => {})} style={{
                  fontSize: 11, padding: "5px 14px", borderRadius: 6, fontWeight: 600, cursor: "pointer",
                  background: speakerMuted ? "rgba(52,211,153,0.1)" : "rgba(239,68,68,0.08)",
                  border: `1px solid ${speakerMuted ? "rgba(52,211,153,0.3)" : "rgba(239,68,68,0.25)"}`,
                  color: speakerMuted ? "var(--lm-green)" : "#f87171",
                }}>
                  {speakerMuted ? "Unmute" : "Mute"}
                </button>
              </div>

              {/* Volume slider */}
              <div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Volume</span>
                  <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
                    {localVolume ?? audio?.volume ?? "—"}%
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={localVolume ?? audio?.volume ?? 50}
                  onChange={(e) => {
                    draggingVolume.current = true;
                    setLocalVolume(Number(e.target.value));
                  }}
                  onMouseUp={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  onTouchEnd={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  style={{
                    width: "100%", height: 6, cursor: "pointer",
                    accentColor: "var(--lm-amber)",
                  }}
                />
              </div>
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>
      </div>

      {/* Row 2: Emotion + Hardware + Scene + Servo Pose */}
      <div className="lm-grid-4">
        {/* Emotion */}
        <div style={{
          ...S.card, padding: "14px 16px",
          background: emotion ? `linear-gradient(135deg, var(--lm-bg) 60%, ${emotionColor}18)` : "var(--lm-bg)",
          border: `1px solid ${emotion ? emotionColor + "55" : "var(--lm-border)"}`,
          transition: "all 0.4s ease",
        }}>
          <div style={S.cardLabel}>Emotion</div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{
              fontSize: 36, lineHeight: 1, flexShrink: 0,
              filter: emotion ? `drop-shadow(0 0 8px ${emotionColor}88)` : "none",
              transition: "filter 0.4s ease",
            }}>
              {emotion ? emotionEmoji : "✦"}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 2, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Lumi is feeling
              </div>
              <div style={{ fontSize: 18, fontWeight: 700, color: emotion ? emotionColor : "var(--lm-text-muted)", textTransform: "capitalize", transition: "color 0.4s ease" }}>
                {emotion || "—"}
              </div>
            </div>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4, marginTop: 10 }}>
            {ALL_EMOTIONS.map((e) => {
              const active = e === emotion;
              const c = EMOTION_COLOR[e] ?? "#fff";
              return (
                <span key={e} role="button" title={`Test emotion: ${e}`} onClick={() => {
                  fetch(`${HW}/emotion`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ emotion: e, intensity: 1.0 }),
                  }).catch(() => {});
                }} style={{
                  fontSize: 9.5, padding: "1px 6px", borderRadius: 8,
                  background: active ? `${c}22` : "var(--lm-surface)",
                  border: `1px solid ${active ? c + "88" : "var(--lm-border)"}`,
                  color: active ? c : "var(--lm-text-muted)",
                  fontWeight: active ? 700 : 400,
                  textTransform: "capitalize",
                  transition: "all 0.3s ease",
                  cursor: "pointer",
                }}>
                  {EMOTION_EMOJI[e]} {e}
                </span>
              );
            })}
          </div>
        </div>

        {/* Hardware */}
        <div style={S.card}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <div style={S.cardLabel}>Hardware</div>
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
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>

        {/* Scene */}
        <div style={S.card}>
          <div style={S.cardLabel}>Scene</div>
          {sceneInfo ? (
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
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>

        {/* Servo */}
        <div style={S.card}>
          <div style={S.cardLabel}>Servo Pose</div>
          {servo ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-amber)" }}>
                {servo.current || "idle"}
                {(servo.bus_connected === false || servo.robot_connected === false) && (
                  <span style={{ fontSize: 10, color: "var(--lm-danger, #c44)", marginLeft: 6 }}>
                    (bus {servo.bus_connected === false ? "down" : "ok"}{servo.robot_connected === false ? ", robot off" : ""})
                  </span>
                )}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4 }}>
                {(servo.available_recordings ?? []).map((p) => (
                  <span key={p} role="button" onClick={() => {
                    fetch(`${HW}/servo/play`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ recording: p }),
                    }).catch(() => {});
                  }} style={{
                    fontSize: 10, padding: "2px 6px", borderRadius: 4,
                    background: p === servo.current ? "var(--lm-amber-dim)" : "var(--lm-surface)",
                    border: `1px solid ${p === servo.current ? "var(--lm-amber)" : "var(--lm-border)"}`,
                    color: p === servo.current ? "var(--lm-amber)" : "var(--lm-text-dim)",
                    cursor: "pointer",
                  }}>{p}</span>
                ))}
              </div>
              <button onClick={() => {
                fetch(`${HW}/servo/release`, { method: "POST", headers: { accept: "application/json" } }).catch(() => {});
              }} style={{
                marginTop: 2, fontSize: 10, padding: "3px 9px", borderRadius: 4,
                background: "var(--lm-surface)", border: "1px solid var(--lm-border)",
                color: "var(--lm-text-dim)", cursor: "pointer",
              }}>Release</button>
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
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

      {/* Versions + Lamp Buddy pairing.
          OS uptime sits in the host row; detailed CPU/RAM/Disk live in System tab. */}
      <div className="lm-grid-4">
        <div style={S.card}>
          <div style={{ ...S.cardLabel, marginBottom: 10 }}>Versions</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <VersionRow name="Host"   color="var(--lm-text)"   version={null}                    uptime={sys?.uptime ?? null}                                   updateTarget={null} />
            <VersionRow name="Web"    color="var(--lm-teal)"   version={webVersion}              uptime={null}                                                  updateTarget={isDebug ? "web" : null} />
            <VersionRow name="Lumi"   color="var(--lm-amber)"  version={sys?.version ?? null}    uptime={sys?.serviceUptime ?? null}                            updateTarget={isDebug ? "lumi" : null} />
            <VersionRow name="LeLamp" color="var(--lm-blue)"   version={lelampVersion}           uptime={sys?.lelampUptime ?? null}                             updateTarget={isDebug ? "lelamp" : null} />
            <VersionRow name="Agent"  color="var(--lm-purple)" version={oc?.version ?? null}     uptime={oc?.connected ? (oc?.agentUptime ?? null) : null}      updateTarget={null} />
          </div>
        </div>
        <BuddyCard />
      </div>

    </div>
  );
}

function VersionRow({ name, color, version, uptime, updateTarget }: {
  name: string;
  color: string;
  version: string | null;
  uptime: number | null;
  updateTarget: "lumi" | "web" | "lelamp" | null;
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
