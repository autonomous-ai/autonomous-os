import { C, SectionCard } from "./shared";
import { testTTSVoice } from "@/lib/api";

export function TTSSection({
  active, isContinue,
  ttsProvider, setTtsProvider, ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  sttLanguage,
}: {
  active: boolean;
  isContinue: boolean;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  sttLanguage: string;
}) {
  return (
    <SectionCard id="tts" title="Voice" active={active}>
      {/* tts_api_key + tts_base_url are not exposed in Setup —
          they're auto-mirrored from AI Brain via useEffect and
          submitted silently. */}
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_provider" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
          Provider
        </label>
        <select
          id="tts_provider"
          value={ttsProvider}
          onChange={(e) => setTtsProvider(e.target.value)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          {(ttsProviders.length > 0 ? ttsProviders : ["openai"]).map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_voice" style={{ display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 }}>
          Voice
        </label>
        <select
          id="tts_voice"
          value={ttsVoice}
          onChange={(e) => setTtsVoice(e.target.value)}
          style={{
            width: "100%", boxSizing: "border-box",
            background: C.surface, border: `1px solid ${C.border}`,
            borderRadius: 7, padding: "8px 11px",
            fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
          }}
        >
          {(ttsVoices.length > 0 ? ttsVoices : ["alloy"]).map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
        {isContinue ? (
          <button
            type="button"
            onClick={() => testTTSVoice(ttsVoice, {
              lang: sttLanguage,
              provider: ttsProvider,
            })}
            style={{
              marginTop: 8, width: "100%", padding: "8px 0",
              background: C.amber, color: "#fff", border: "none",
              borderRadius: 7, fontSize: 12, cursor: "pointer", fontWeight: 600,
            }}
          >
            Test Voice
          </button>
        ) : (
          <div style={{ marginTop: 8, fontSize: 11, color: C.textDim }}>
            You can preview voices after your device is online (next step).
          </div>
        )}
      </div>
    </SectionCard>
  );
}
