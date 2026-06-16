import { useRef, useState } from "react";
import { Mic } from "lucide-react";
import { C, Field, SectionCard } from "./shared";
import { pickVoicePhrases, pickVoiceIntro, VOICE_DURATION_SEC } from "./voice-phrases";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";

// Voice enrollment lives inside this section component — state is local since
// nothing outside the section reads it. After each enroll we call
// loadFaceOwners so the new sample shows up in the enrolled list.

export function VoiceSection({
  active, sttLanguage, faceOwners, loadFaceOwners,
}: {
  active: boolean;
  sttLanguage: string;
  faceOwners: FaceOwner[];
  loadFaceOwners: () => Promise<void>;
}) {
  const VOICE_PHRASES = pickVoicePhrases(sttLanguage);
  const VOICE_INTRO = pickVoiceIntro(sttLanguage);
  const [voiceLabel, setVoiceLabel] = useState("");
  const [voicePhase, setVoicePhase] = useState<"idle" | "countdown" | "recording" | "processing">("idle");
  const [voiceCountdown, setVoiceCountdown] = useState(0);
  const [voiceMsg, setVoiceMsg] = useState<string | null>(null);
  const voiceTickRef = useRef<number | null>(null);
  const [voiceExpanded, setVoiceExpanded] = useState<Record<string, boolean>>({});
  const toggleVoiceExpanded = (label: string) =>
    setVoiceExpanded((prev) => ({ ...prev, [label]: !prev[label] }));

  const removeVoiceFile = async (name: string, file: string) => {
    if (!confirm(`Delete voice sample "${file}" for "${name}"?`)) return;
    try {
      await fetch("/api/voice/file/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, file }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  };

  const startVoiceEnroll = () => {
    if (!voiceLabel.trim()) {
      setVoiceMsg("Enter a name first");
      return;
    }
    setVoiceMsg(null);
    setVoicePhase("countdown");
    let pre = 3;
    setVoiceCountdown(pre);
    voiceTickRef.current = window.setInterval(() => {
      pre -= 1;
      if (pre > 0) {
        setVoiceCountdown(pre);
        return;
      }
      if (voiceTickRef.current) clearInterval(voiceTickRef.current);
      setVoicePhase("recording");
      let remaining = VOICE_DURATION_SEC;
      setVoiceCountdown(remaining);
      voiceTickRef.current = window.setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("processing");
          setVoiceCountdown(0);
        } else {
          setVoiceCountdown(remaining);
        }
      }, 1000);
      fetch("/api/hardware/speaker/record-enroll", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: voiceLabel.trim().toLowerCase(), duration_sec: VOICE_DURATION_SEC }),
      })
        .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("idle");
          setVoiceCountdown(0);
          if (ok && data.status === "ok") {
            setVoiceMsg(`Enrolled "${voiceLabel.trim().toLowerCase()}"`);
            loadFaceOwners();
          } else {
            setVoiceMsg(`Error: ${data.detail ?? data.message ?? "enroll failed"}`);
          }
        })
        .catch((e) => {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("idle");
          setVoiceCountdown(0);
          setVoiceMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
        });
    }, 1000);
  };

  const enrolled = faceOwners.filter((p) => (p.voice_samples?.length ?? 0) > 0);

  return (
    <SectionCard id="voice" title="My Voice (optional)" active={active} description={VOICE_INTRO} icon={<Mic size={17} />}>
      <Field label="Name" id="voice_label" value={voiceLabel} onChange={setVoiceLabel} placeholder="e.g. Leo" />
      <div style={{
        background: C.surface, border: `1px solid ${C.border}`,
        borderRadius: 8, padding: "12px 14px", marginBottom: 12, fontSize: 13, lineHeight: 1.5,
      }}>
        {VOICE_PHRASES.map((p, i) => (
          <div key={i} style={{ marginBottom: i < VOICE_PHRASES.length - 1 ? 6 : 0 }}>
            <span style={{ color: C.textMuted, marginRight: 6 }}>{i + 1}.</span>
            <span style={{ color: C.text }}>{p}</span>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <button
          type="button" disabled={voicePhase !== "idle"}
          onClick={startVoiceEnroll}
          style={{
            flex: 1, padding: "10px 0",
            background: voicePhase === "idle" ? C.amber : C.surface,
            color: voicePhase === "idle" ? "#0C0B09" : C.textDim,
            border: "none", borderRadius: 8, fontSize: 13,
            cursor: voicePhase === "idle" ? "pointer" : "default", fontWeight: 600,
          }}
        >
          {voicePhase === "idle" && "Start recording"}
          {voicePhase === "countdown" && `Get ready... ${voiceCountdown}`}
          {voicePhase === "recording" && `Recording... ${voiceCountdown}s`}
          {voicePhase === "processing" && "Processing..."}
        </button>
      </div>
      {voiceMsg && (
        <div style={{ fontSize: 11, color: voiceMsg.startsWith("Error") ? C.red : C.green, marginTop: 4 }}>
          {voiceMsg}
        </div>
      )}
      {enrolled.length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${C.border}` }}>
          <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8 }}>Enrolled:</div>
          {enrolled.map((p) => (
            <div key={p.label} style={{ marginBottom: 6 }}>
              <button type="button" onClick={() => toggleVoiceExpanded(p.label)} style={{
                background: "none", border: "none", color: C.text, fontSize: 12,
                cursor: "pointer", padding: 0, display: "flex", alignItems: "center", gap: 6,
              }}>
                <span>{voiceExpanded[p.label] ? "▾" : "▸"}</span>
                {p.label} <span style={{ color: C.textMuted }}>({p.voice_samples?.length ?? 0})</span>
              </button>
              {voiceExpanded[p.label] && (p.voice_samples?.length ?? 0) > 0 && (
                <div style={{ marginLeft: 18, marginTop: 4 }}>
                  {p.voice_samples!.map((file) => (
                    <div key={file} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 11, color: C.textDim, padding: "3px 0" }}>
                      <span>{file}</span>
                      <button type="button" onClick={() => removeVoiceFile(p.label, file)} style={{
                        background: "none", border: "none", color: C.red, cursor: "pointer", fontSize: 11,
                      }}>remove</button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
}
