import { Volume2 } from "lucide-react";
import { C, SectionCard, LABEL_STYLE, INPUT_STYLE, FIELD_GAP } from "./shared";
import { testTTSVoice } from "@/lib/api";

export function TTSSection({
  active, isContinue,
  ttsProvider, setTtsProvider, ttsProviders,
  ttsVoice, setTtsVoice, ttsVoices,
  ttsModel, setTtsModel,
  sttLanguage,
}: {
  active: boolean;
  isContinue: boolean;
  ttsProvider: string; setTtsProvider: (v: string) => void;
  ttsProviders: string[];
  ttsVoice: string; setTtsVoice: (v: string) => void;
  ttsVoices: string[];
  ttsModel: string; setTtsModel: (v: string) => void;
  sttLanguage: string;
}) {
  return (
    <SectionCard id="tts" title="Voice" active={active} icon={<Volume2 size={17} />}
      description="Choose how your device sounds when it speaks back to you.">
      {/* tts_api_key + tts_base_url are not exposed in Setup —
          they're auto-mirrored from AI Brain via useEffect and
          submitted silently. */}
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="tts_provider" style={LABEL_STYLE}>
          Provider
        </label>
        <select
          id="tts_provider"
          value={ttsProvider}
          onChange={(e) => setTtsProvider(e.target.value)}
          style={{ ...INPUT_STYLE, cursor: "pointer" }}
        >
          {(ttsProviders.length > 0 ? ttsProviders : ["elevenlabs"]).map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>
      {ttsProvider === "openai" && (
        <div style={{ marginBottom: FIELD_GAP }}>
          <label htmlFor="tts_model" style={LABEL_STYLE}>
            TTS model
          </label>
          <input
            id="tts_model"
            type="text"
            value={ttsModel}
            onChange={(e) => setTtsModel(e.target.value)}
            placeholder="tts-1"
            style={INPUT_STYLE}
          />
        </div>
      )}
      <div style={{ marginBottom: FIELD_GAP }}>
        <label htmlFor="tts_voice" style={LABEL_STYLE}>
          Voice
        </label>
        {ttsProvider === "openai" ? (
          // OpenAI-compatible endpoints are BYO: the server can only list the
          // voices of the host already saved in config, and mid-wizard nothing
          // is saved yet — so it returns the static OpenAI names. Offer those
          // as suggestions but always let the operator type, otherwise a
          // custom backend's voice (e.g. an oMLX one) is unreachable here.
          <>
            <input
              id="tts_voice"
              type="text"
              list="tts_voice_options"
              value={ttsVoice}
              onChange={(e) => setTtsVoice(e.target.value)}
              placeholder="alloy"
              style={INPUT_STYLE}
            />
            <datalist id="tts_voice_options">
              {ttsVoices.map((v) => (
                <option key={v} value={v} />
              ))}
            </datalist>
          </>
        ) : ttsVoices.length > 0 ? (
          <select
            id="tts_voice"
            value={ttsVoice}
            onChange={(e) => setTtsVoice(e.target.value)}
            style={{ ...INPUT_STYLE, cursor: "pointer" }}
          >
            {ttsVoices.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        ) : (
          // No server-known voice list for this provider/endpoint (e.g. a
          // custom OpenAI-compatible base URL the server couldn't probe) —
          // let the operator type the voice name directly instead of
          // offering a hardcoded list that may not exist on their backend.
          <input
            id="tts_voice"
            type="text"
            value={ttsVoice}
            onChange={(e) => setTtsVoice(e.target.value)}
            placeholder="Rachel"
            style={INPUT_STYLE}
          />
        )}
        {isContinue ? (
          <button
            type="button"
            onClick={() => testTTSVoice(ttsVoice, {
              lang: sttLanguage,
              provider: ttsProvider,
            })}
            style={{
              marginTop: 10, width: "100%", padding: "10px 0",
              background: C.amber, color: "#0C0B09", border: "none",
              borderRadius: 8, fontSize: 13, cursor: "pointer", fontWeight: 600,
            }}
          >
            Test Voice
          </button>
        ) : (
          <div style={{ marginTop: 8, fontSize: 12.5, color: C.textDim }}>
            You can preview voices after your device is online (next step).
          </div>
        )}
      </div>
    </SectionCard>
  );
}
