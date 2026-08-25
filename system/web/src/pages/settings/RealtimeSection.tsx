import { useEffect, useState } from "react";
import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { getRealtimeOptions } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";
import { REASONING, VOICES } from "@/pages/settings/realtimeKnobs";

// Realtime voice-agent config. Values map 1:1 to the config.json `realtime`
// block (HAL reads it; os-server restarts HAL on save). Voice + reasoning are
// provider-specific — keep these lists in sync with
// system/server/config/realtime.go (ValidateRealtimeKnobs) and the HAL enums.
// pipecat and cascaded are the odd ones out: both drive the LLM half of the
// turn from the STT transcript rather than being audio-native, so neither has a
// voice/reasoning knob and both point at a plain OpenAI-compatible /v1 host.
// They share the realtime.pipecat.* block, so switching between them keeps the
// endpoint — cascaded is the same brain without the pipecat dependency.
const PROVIDERS = ["gemini", "openai", "qwen", "pipecat", "cascaded", "none"];

// Display labels for the Provider dropdown. Values on the wire stay lowercase
// (server-side switch keys off "gemini" / "openai" / …); only the human-facing
// string is title-cased. Unknown providers fall back to first-letter capitalise
// so the UI never shows a raw lowercase entry.
const PROVIDER_LABEL: Record<string, string> = {
  gemini: "Gemini",
  openai: "OpenAI",
  qwen: "Qwen",
  pipecat: "Pipecat",
  cascaded: "Cascaded",
  none: "None",
};
const displayProvider = (v: string): string =>
  PROVIDER_LABEL[v] ?? (v ? v[0].toUpperCase() + v.slice(1) : v);
export interface RealtimeLoadedState {
  apiKey: boolean;
}

const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};

const labelStyle = { display: "block", fontSize: 11, color: C.textDim, marginBottom: 5 };

export function RealtimeSection({
  active,
  realtimeLoaded, llmLoaded,
  enabled, setEnabled,
  provider, setProvider,
  voice, setVoice,
  reasoning, setReasoning,
  apiKey, setApiKey,
  baseUrl, setBaseUrl,
}: {
  active: boolean;
  realtimeLoaded: RealtimeLoadedState;
  llmLoaded: LlmLoadedState;
  enabled: boolean; setEnabled: (v: boolean) => void;
  provider: string; setProvider: (v: string) => void;
  voice: string; setVoice: (v: string) => void;
  reasoning: string; setReasoning: (v: string) => void;
  apiKey: string; setApiKey: (v: string) => void;
  baseUrl: string; setBaseUrl: (v: string) => void;
}) {
  // Options come from the API (single source = server config); the const lists
  // above are only a fallback if the fetch fails.
  const [opts, setOpts] = useState<{ providers: string[]; voices: Record<string, string[]>; reasoning: Record<string, string[]> } | null>(null);
  useEffect(() => { getRealtimeOptions().then(setOpts).catch(() => {}); }, []);
  const providers = opts?.providers ?? PROVIDERS;
  const voices = (opts?.voices ?? VOICES)[provider] ?? [];
  const reasonings = (opts?.reasoning ?? REASONING)[provider] ?? [];
  // The two cascaded brains share realtime.pipecat.* — and its base_url no
  // longer derives from the AI brain, so the field's copy differs for them.
  const cascaded = provider === "pipecat" || provider === "cascaded";

  // Switching provider resets voice/reasoning to that provider's defaults so we
  // never submit, e.g., an OpenAI voice while provider=gemini (server rejects it).
  function onProviderChange(p: string) {
    setProvider(p);
    if (p === "none") return;
    if (!(VOICES[p] ?? []).includes(voice)) setVoice((VOICES[p] ?? [])[0] ?? "");
    if (!(REASONING[p] ?? []).includes(reasoning)) setReasoning((REASONING[p] ?? [])[0] ?? "");
  }

  return (
    <SectionCard id="realtime" title="Realtime" active={active}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, cursor: "pointer", fontSize: 12.5, color: C.text }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled (audio-native — Gemini Live / OpenAI Realtime / Qwen Omni — or cascaded Pipecat)
      </label>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="realtime_provider" style={labelStyle}>Provider</label>
        <select id="realtime_provider" value={provider} onChange={(e) => onProviderChange(e.target.value)} style={selectStyle}>
          {providers.map((p) => <option key={p} value={p}>{displayProvider(p)}</option>)}
        </select>
      </div>

      {provider !== "none" && (
        <>
          {/* Realtime voice output is NOT used (device speaks via TTS), so the
              voice selector is hidden via display:none — code kept for re-enable. */}
          <div style={{ marginBottom: 12, display: "none" }}>
            <label htmlFor="realtime_voice" style={labelStyle}>Voice</label>
            <select id="realtime_voice" value={voice} onChange={(e) => setVoice(e.target.value)} style={selectStyle}>
              {voices.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </div>

          {reasonings.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <label htmlFor="realtime_reasoning" style={labelStyle}>Reasoning (cost — cheapest first)</label>
              <select id="realtime_reasoning" value={reasoning} onChange={(e) => setReasoning(e.target.value)} style={selectStyle}>
                {reasonings.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          )}

          <LockedPasswordField lockedInitially={realtimeLoaded.apiKey || llmLoaded.apiKey} label="API Key (optional — leave blank to reuse AI brain key)" id="realtime_api_key" value={apiKey} onChange={setApiKey} placeholder="sk-... / AIza..." />
          <LockedField lockedInitially={llmLoaded.baseUrl} label={cascaded ? "Base URL (optional — leave blank for the autonomous qwen route)" : "Base URL (optional — leave blank to derive from AI brain base URL)"} id="realtime_base_url" value={baseUrl} onChange={setBaseUrl} placeholder={cascaded ? "https://campaign-api.autonomous.ai/api/v1/ai/v1/qwen/v1" : "wss://… /ws/gemini"} />
        </>
      )}
    </SectionCard>
  );
}
