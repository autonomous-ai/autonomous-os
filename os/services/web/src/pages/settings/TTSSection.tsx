import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { testTTSVoice } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";

export interface TtsLoadedState {
  apiKey: boolean;
  baseUrl: boolean;
}

// Edit-mode TTS exposes the api key + base URL fields so operators can override
// them per-section. Setup hides those because they auto-mirror from AI Brain.
export function TTSSection({
  active,
  ttsLoaded, llmLoaded,
  ttsApiKey, setTtsApiKey,
  ttsBaseUrl, setTtsBaseUrl,
  ttsProvider, setTtsProvider, ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  sttLanguage,
}: {
  active: boolean;
  ttsLoaded: TtsLoadedState;
  llmLoaded: LlmLoadedState;
  ttsApiKey: string; setTtsApiKey: (v: string) => void;
  ttsBaseUrl: string; setTtsBaseUrl: (v: string) => void;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  sttLanguage: string;
}) {
  return (
    <SectionCard id="tts" title="Voice" active={active}>
      <LockedPasswordField lockedInitially={ttsLoaded.apiKey || llmLoaded.apiKey} label="API Key (optional — leave blank to reuse AI brain key)" id="tts_api_key" value={ttsApiKey} onChange={setTtsApiKey} placeholder="sk-..." />
      <LockedField lockedInitially={ttsLoaded.baseUrl || llmLoaded.baseUrl} label="Base URL (optional — leave blank to reuse AI brain base URL)" id="tts_base_url" value={ttsBaseUrl} onChange={setTtsBaseUrl} placeholder="https://api.openai.com/v1" />
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
      </div>
    </SectionCard>
  );
}
