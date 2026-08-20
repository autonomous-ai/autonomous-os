import { useEffect, useState } from "react";
import { Loader2, Volume2, Check, AlertCircle } from "lucide-react";
import { C, LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";
import { testTTSVoice } from "@/lib/api";
import type { LlmLoadedState } from "@/hooks/setup/types";

export interface TtsLoadedState {
  apiKey: boolean;
  baseUrl: boolean;
}

// Provider "choice" is a UI-only construct — it groups the two on-disk fields
// (`tts_provider` + `tts_base_url`) into one dropdown the operator picks first,
// so they can't accidentally paste a Deepgram STT URL under an ElevenLabs
// provider (a real bug reported: /listen is STT, TTS wants /speak, so
// Test Voice silently 404'd). Save-time the choice is decomposed back into
// the two on-wire fields — the backend contract does not change.
//
// Autonomous is special: it's a routing hub whose proxy path decides which
// vendor the request is billed to. So picking "Autonomous" also asks for a
// vendor (OpenAI or ElevenLabs) — that vendor becomes `tts_provider` while
// `tts_base_url` stays the autonomous.ai campaign-api endpoint. Backend
// implementations (see hal/drivers/voice/tts/elevenlabs.py) detect the
// autonomous.ai host and append the `/elevenlabs` proxy path; direct-API
// hosts skip that prefix.
// Deepgram is intentionally NOT in this list: HAL's TTS registry
// (hal/drivers/voice/tts/backend.py) only implements the `openai` and
// `elevenlabs` backends — picking "deepgram" silently falls back to the
// OpenAI backend, which produces a burst of silent audio (no error). Leave
// Deepgram out until a real Deepgram TTS backend lands, so the operator
// can't paint themselves into a dead-end. STT works with Deepgram on a
// different code path (this is TTS-only).
type ProviderChoice = "autonomous" | "openai" | "elevenlabs" | "custom";

// Vendor covers only the choices that have a distinct audio backend on disk.
// Autonomous supports two vendors; every other choice has exactly one vendor
// (matching its provider name).
type Vendor = "openai" | "elevenlabs";

interface ChoiceMeta {
  label: string;
  baseUrl: string;   // pinned URL; blank for custom (user picks)
  vendor?: Vendor;   // omitted for autonomous (asks separately) and custom
  hint?: string;
}
const CHOICES: Record<ProviderChoice, ChoiceMeta> = {
  autonomous: {
    label: "Autonomous (proxy)",
    baseUrl: "https://campaign-api.autonomous.ai/api/v1/ai/v1",
    // Vendor deliberately UNSET — Autonomous routes to two backends and the
    // operator picks between them via the sub-picker below. The MQTT path
    // that web.autonomous.ai uses can drive OpenAI TTS through this proxy
    // successfully, so exposing OpenAI here is required even though a
    // direct probe of POST /audio/speech from the device sometimes returns
    // a chatcmpl-error body (key-tier issue; the endpoint is real).
    hint: "Routes through Autonomous — supports OpenAI + ElevenLabs voices",
  },
  openai: {
    label: "OpenAI (direct)",
    baseUrl: "https://api.openai.com/v1",
    vendor: "openai",
  },
  elevenlabs: {
    label: "ElevenLabs (direct)",
    baseUrl: "https://api.elevenlabs.io/v1",
    vendor: "elevenlabs",
  },
  custom: {
    label: "Custom (BYO URL)",
    baseUrl: "",
    hint: "Bring-your-own URL — for self-hosted proxies (vLLM, LiteLLM, …)",
  },
};

// Detect the current choice from persisted (provider, baseUrl). Anchor on the
// URL host — the raw provider is preserved as the vendor sub-select when the
// choice is Autonomous. If nothing matches a preset we call it Custom rather
// than mis-labelling as Autonomous — an operator with a self-hosted URL
// should see "Custom", not "Autonomous", so switching provider doesn't
// silently overwrite their URL with the campaign-api endpoint.
function detectChoice(baseUrl: string): ProviderChoice {
  let host = "";
  try { host = new URL(baseUrl).hostname.toLowerCase(); } catch { /* invalid — fall through */ }
  if (!host) return "autonomous";  // empty URL = default to the proxy (matches "leave blank → reuse AI brain")
  if (host.endsWith("autonomous.ai") || host.endsWith("autonomousdev.xyz")) return "autonomous";
  if (host === "api.openai.com") return "openai";
  if (host === "api.elevenlabs.io" || host.endsWith(".elevenlabs.io")) return "elevenlabs";
  // api.deepgram.com hits this branch too — HAL has no Deepgram TTS backend
  // so an existing config that somehow ended up on this host lands in Custom
  // and the operator can pick a real vendor to swap to.
  return "custom";
}

// Voices are provider-scoped: OpenAI's "alloy" doesn't exist on ElevenLabs.
// The full catalog comes from `ttsVoices` (server-derived), but that list is
// often for ALL providers mixed. When we know the vendor we prefer the
// hand-curated per-vendor list so the picker only shows names the vendor
// actually accepts — the server-side test would 404 otherwise.
// Supported voice languages. Mirrors hal/presets.py::SUPPORTED_LANGS and
// hal/drivers/voice/tts/elevenlabs.py's language bucket routing (zh-CN and
// zh-TW share the same voice pool). "" = auto (falls back to the current
// sttLanguage; no filtering).
type Lang = "" | "en" | "vi" | "zh-CN" | "zh-TW";
const LANG_LABEL: Record<Lang, string> = {
  "":      "Auto (follow device language)",
  "en":    "English",
  "vi":    "Vietnamese",
  "zh-CN": "Chinese (Simplified)",
  "zh-TW": "Chinese (Traditional)",
};
const LANG_OPTIONS: Lang[] = ["", "en", "vi", "zh-CN", "zh-TW"];

// Voice pools split by (vendor, language bucket). ElevenLabs voice NAMES
// are resolved to voice_ids server-side by HAL's mapping
// (hal/drivers/voice/tts/elevenlabs.py::VOICE_IDS_BY_LANG). Names not in
// that mapping get passed literally to the API and 404 with voice_not_found
// — keep this list a strict subset of HAL's mapping so every pick is
// guaranteed to resolve.
//
// OpenAI's TTS voices are language-agnostic (the model handles multilingual
// input with the same voice IDs), so the same list serves every language.
type LangBucket = "en" | "vi" | "zh";
function langBucket(lang: Lang): LangBucket {
  if (lang === "vi") return "vi";
  if (lang === "zh-CN" || lang === "zh-TW") return "zh";
  return "en";  // "" (auto) resolves via sttLanguage → this default is safe
}
const OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"];
const VOICES: Record<Vendor, Record<LangBucket, string[]>> = {
  elevenlabs: {
    en: [
      // Female — top picks
      "Rachel", "Sarah", "Nicole", "Terra", "Maria", "Sophie",
      "Piper", "Mia", "Kimmy", "Brianna", "Ally", "Tori",
      // Male — top picks
      "Brian", "Adam", "Daniel", "George", "James", "Liam",
      "Charlie", "Sam", "Sean", "Kael", "Brooks", "Erion",
    ],
    vi: ["Ngan", "Linh", "Huyen", "Freya", "Nathan"],
    zh: ["Amy", "Sage", "Xiaoxi", "Yun", "Evan Zhao"],
  },
  openai: { en: OPENAI_VOICES, vi: OPENAI_VOICES, zh: OPENAI_VOICES },
};

function voicesFor(vendor: Vendor, lang: Lang, sttLang: string): string[] {
  // "auto" (empty lang) falls back to the device's STT language so the TTS
  // voice list stays consistent with what the operator hears the device
  // recognise. Guards against a mis-typed sttLanguage by dropping through
  // to English.
  const effective = lang || (sttLang as Lang) || "en";
  return VOICES[vendor][langBucket(effective)];
}

// Display label for a voice name. Values on the wire stay whatever the vendor
// expects (OpenAI + Deepgram use lowercase IDs; ElevenLabs uses TitleCase),
// but the picker capitalises the first letter and cleans up hyphens so the
// UI reads consistently — no "alloy" beside "Rachel". Value posted to the
// server is unchanged; this only affects rendering.
function displayVoice(v: string): string {
  if (!v) return v;
  // For hyphen-separated IDs (Deepgram aura-asteria-en), keep the first token
  // capitalised and drop the trailing lang suffix so the picker is readable.
  const parts = v.split("-");
  if (parts.length > 1) {
    const named = parts.slice(0, 2).map((p, i) =>
      i === 0 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1)
    ).join(" ");
    return named;
  }
  return v[0].toUpperCase() + v.slice(1);
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
  // Choice is stored as state (not derived) so the operator can pick
  // "Custom (BYO URL)" while the on-disk URL still matches a preset host —
  // a pure derivation would snap the dropdown back to that preset the
  // moment we let the click land, because the operator hasn't typed a new
  // URL yet. Initialised from the URL on first mount; re-synced when the
  // URL changes externally (config save, prop reload) but NOT when the
  // current choice is "custom" (they own the URL then, don't yank them).
  const [choice, setChoice] = useState<ProviderChoice>(() => detectChoice(ttsBaseUrl));
  useEffect(() => {
    setChoice((prev) => (prev === "custom" ? prev : detectChoice(ttsBaseUrl)));
  }, [ttsBaseUrl]);
  const meta = CHOICES[choice];

  // Vendor for the current choice. Autonomous defers to the on-disk
  // tts_provider (openai / elevenlabs); every other preset pins vendor via
  // meta.vendor; custom asks the operator to choose.
  const vendor: Vendor = meta.vendor
    ?? (ttsProvider === "openai" || ttsProvider === "elevenlabs"
      ? (ttsProvider as Vendor)
      : "elevenlabs");

  // Language picker — local state (not persisted server-side). Voice list
  // filters by this; empty means "follow the device's STT language".
  // ElevenLabs voice pools differ per language (Rachel is English, Ngan is
  // Vietnamese, Amy is Chinese) — surfacing the language selector lets the
  // operator pick a Vietnamese voice without the picker being cluttered by
  // English names, and vice-versa. OpenAI's voices are language-agnostic,
  // so the language dropdown is a no-op there (voice list stays the same).
  const [lang, setLang] = useState<Lang>("");
  const voices = voicesFor(vendor, lang, sttLanguage);

  const onChoice = (next: ProviderChoice) => {
    setChoice(next);   // always commit the pick — even Custom, so the picker doesn't snap back
    const nextMeta = CHOICES[next];
    // Custom: leave URL as-is if there was one — clearing would wipe a
    // useful value; the URL input is editable so the operator picks up
    // where they were. Vendor: keep whatever was on disk; the Custom vendor
    // sub-picker below lets them flip protocol.
    if (next === "custom") {
      return;
    }
    setTtsBaseUrl(nextMeta.baseUrl);
    if (next === "autonomous") {
      // Autonomous supports 2 vendors — preserve the current vendor if it's
      // already OpenAI/ElevenLabs; else default to ElevenLabs (the
      // historical default that the proxy has always accepted).
      if (ttsProvider !== "openai" && ttsProvider !== "elevenlabs") {
        setTtsProvider("elevenlabs");
        setTtsVoice(voicesFor("elevenlabs", lang, sttLanguage)[0]);
      }
      // Clear any stale vendor-scoped key (e.g. an ElevenLabs `sk_...`
      // left over from a previous ElevenLabs-direct config) so the
      // backend's GetTTSAPIKey fallback picks up llm_api_key (the JWT the
      // proxy authenticates against). Without this, a stale key silently
      // 401s the proxy call and Test Voice comes out as ~6ms of silence.
      setTtsApiKey("");
      return;
    }
    // Direct presets pin a vendor via nextMeta.vendor. Sync provider + reset
    // voice when the vendor changes so the voice picker doesn't offer names
    // that don't exist on the new vendor.
    if (nextMeta.vendor && nextMeta.vendor !== ttsProvider) {
      setTtsProvider(nextMeta.vendor);
      setTtsVoice(voicesFor(nextMeta.vendor, lang, sttLanguage)[0]);
    }
  };

  const onVendor = (v: Vendor) => {
    setTtsProvider(v);
    setTtsVoice(voicesFor(v, lang, sttLanguage)[0]);
  };

  const onLang = (next: Lang) => {
    setLang(next);
    // Snap voice to a valid choice for the new language: if the current
    // voice isn't in the target pool, jump to the first entry. Otherwise
    // keep the operator's pick (they may want to hop between languages
    // without losing "Rachel").
    const pool = voicesFor(vendor, next, sttLanguage);
    if (!pool.includes(ttsVoice) && pool.length > 0) {
      setTtsVoice(pool[0]);
    }
  };

  return (
    <SectionCard id="tts" title="Voice" active={active}>
      {/* 1. Provider FIRST — decides URL + vendor. Read-only URL below
          removes the "I pasted a wrong URL" foot-gun. */}
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_provider_choice" style={labelStyle}>Provider</label>
        <select
          id="tts_provider_choice"
          value={choice}
          onChange={(e) => onChoice(e.target.value as ProviderChoice)}
          style={selectStyle}
        >
          {(Object.keys(CHOICES) as ProviderChoice[]).map((k) => (
            <option key={k} value={k}>{CHOICES[k].label}</option>
          ))}
        </select>
        {meta.hint && (
          <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 4 }}>{meta.hint}</div>
        )}
      </div>

      {/* 2. Vendor sub-picker — only when Autonomous. Other presets pin
          vendor via their meta.vendor. */}
      {choice === "autonomous" && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="tts_vendor" style={labelStyle}>Vendor (voices come from here)</label>
          <select
            id="tts_vendor"
            value={ttsProvider === "openai" ? "openai" : "elevenlabs"}
            onChange={(e) => onVendor(e.target.value as Vendor)}
            style={selectStyle}
          >
            <option value="openai">OpenAI</option>
            <option value="elevenlabs">ElevenLabs</option>
          </select>
        </div>
      )}

      {/* 3. Base URL — read-only for presets, editable ONLY for Custom.
          Preset URLs come from the CHOICES table, not from the operator's
          keyboard — a wrong URL was the reported bug. */}
      {choice === "custom" ? (
        <LockedField
          lockedInitially={ttsLoaded.baseUrl || llmLoaded.baseUrl}
          label="Base URL"
          id="tts_base_url"
          value={ttsBaseUrl}
          onChange={setTtsBaseUrl}
          placeholder="https://your-proxy.example.com/v1"
        />
      ) : (
        <div style={{ marginBottom: 12 }}>
          <label style={labelStyle}>Base URL (locked — set by provider)</label>
          <div style={{
            ...selectStyle,
            cursor: "default", fontFamily: "ui-monospace, monospace",
            color: C.textDim,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{meta.baseUrl}</div>
        </div>
      )}

      {/* 4. Vendor picker for Custom — operator picks the vendor protocol
          their custom proxy speaks (OpenAI-compat / ElevenLabs-compat / …). */}
      {choice === "custom" && (
        <div style={{ marginBottom: 12 }}>
          <label htmlFor="tts_custom_vendor" style={labelStyle}>Vendor protocol (which API your URL speaks)</label>
          <select
            id="tts_custom_vendor"
            value={vendor}
            onChange={(e) => onVendor(e.target.value as Vendor)}
            style={selectStyle}
          >
            <option value="openai">OpenAI-compatible</option>
            <option value="elevenlabs">ElevenLabs-compatible</option>
          </select>
        </div>
      )}

      {/* 5. API Key — always editable. Blank inherits AI brain key. */}
      {/* Label shows a "configured" badge when tts_api_key is on file so
          the operator has visual confirmation the save landed — the actual
          value never leaves the device (server returns has_tts_api_key
          only), and the input starts empty on reload so we can't render
          the real chars. Placeholder shows "•••••••• saved" for the same
          reason: an empty box after a save reads as "nothing was saved". */}
      <div style={{ marginBottom: 5, display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <label htmlFor="tts_api_key" style={labelStyle}>
          API Key (optional — leave blank to reuse AI brain key)
        </label>
        {ttsLoaded.apiKey && (
          <span style={{ fontSize: 10, color: "var(--lm-green, #34d399)", fontWeight: 600 }}>
            ✓ configured
          </span>
        )}
      </div>
      <LockedPasswordField
        lockedInitially={ttsLoaded.apiKey || llmLoaded.apiKey}
        label=""
        id="tts_api_key"
        value={ttsApiKey}
        onChange={setTtsApiKey}
        placeholder={ttsLoaded.apiKey ? "•••••••• saved (click ✎ to rotate)" : "sk-..."}
      />

      {/* 6a. Language — filters the Voice list. Session-local (not saved to
          config). Autonomous(ElevenLabs) has distinct voice pools per
          language (English "Rachel" vs Vietnamese "Ngan" vs Chinese "Amy"),
          so surfacing this picker lets the operator narrow the Voice
          picker without hunting through a mixed list. */}
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_lang" style={labelStyle}>Language (voice list filter)</label>
        <select
          id="tts_lang"
          value={lang}
          onChange={(e) => onLang(e.target.value as Lang)}
          style={selectStyle}
        >
          {LANG_OPTIONS.map((l) => (
            <option key={l || "auto"} value={l}>{LANG_LABEL[l]}</option>
          ))}
        </select>
        {vendor === "openai" && (
          <div style={{ fontSize: 10.5, color: C.textMuted, marginTop: 4 }}>
            OpenAI voices are multilingual — the same voice handles any
            language. Filter is a no-op here.
          </div>
        )}
      </div>

      {/* 6b. Voice — options filter to the vendor AND language so the picker
          doesn't offer names that don't exist on the target combination
          (server would 404). */}
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="tts_voice" style={labelStyle}>Voice</label>
        <select
          id="tts_voice"
          value={voices.includes(ttsVoice) ? ttsVoice : (voices[0] ?? "")}
          onChange={(e) => setTtsVoice(e.target.value)}
          style={selectStyle}
        >
          {(voices.length > 0 ? voices : ttsVoices).map((v) => (
            <option key={v} value={v}>{displayVoice(v)}</option>
          ))}
        </select>
        <TestVoiceButton
          voice={ttsVoice}
          lang={lang || sttLanguage}
          provider={ttsProvider}
          baseUrl={ttsBaseUrl}
          apiKey={ttsApiKey}
        />
      </div>

      {/* Suppress the "unused" lint on ttsProviders — kept in the props
          signature so a future switch back to server-driven list is a
          drop-in swap. */}
      {false && <span>{ttsProviders.join(",")}</span>}
    </SectionCard>
  );
}

// Local button with a 4-state loading feedback loop so a click stops looking
// like the UI is frozen: idle → loading (spinner + "Sending…") → played
// ("Playing on device") for ~2.5s → back to idle. Errors flip to a red
// "Failed" state for the same window. Prior version fired-and-forgot with no
// visual change — the operator saw nothing happen and clicked again.
function TestVoiceButton({ voice, lang, provider, baseUrl, apiKey }: {
  voice: string;
  lang: string;
  provider: string;
  // Pending URL / key from the parent's state — sent alongside the test so
  // the operator's un-saved edits are validated (not the on-disk config).
  // Empty strings fall back to saved config server-side.
  baseUrl: string;
  apiKey: string;
}) {
  type Phase = "idle" | "loading" | "ok" | "error";
  const [phase, setPhase] = useState<Phase>("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const busy = phase === "loading";

  const onClick = async () => {
    if (busy) return;
    setPhase("loading");
    setErrorMsg("");
    try {
      await testTTSVoice(voice, { lang, provider, baseUrl, apiKey });
      setPhase("ok");
      window.setTimeout(() => setPhase("idle"), 2500);
    } catch (err) {
      setPhase("error");
      setErrorMsg(err instanceof Error ? err.message : "Test failed");
      window.setTimeout(() => setPhase("idle"), 3500);
    }
  };

  // Colour + label track the phase. Loading disables clicks so a slow proxy
  // (5s+ TTFB observed on OpenAI TTS via the campaign-api proxy) can't be
  // re-fired mid-request and stack up multiple synths on the same speaker.
  const bg =
    phase === "ok" ? "var(--lm-green, #34d399)" :
    phase === "error" ? "var(--lm-red, #ef4444)" :
    "var(--lm-amber, #f5c25a)";
  const icon =
    phase === "loading" ? <Loader2 size={14} className="lm-spin-ico" /> :
    phase === "ok" ? <Check size={14} /> :
    phase === "error" ? <AlertCircle size={14} /> :
    <Volume2 size={14} />;
  const label =
    phase === "loading" ? "Sending to device…" :
    phase === "ok" ? "Playing on device" :
    phase === "error" ? "Failed" :
    "Test Voice";

  return (
    <>
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        aria-live="polite"
        style={{
          marginTop: 8, width: "100%", padding: "10px 0",
          background: bg, color: "#fff", border: "none",
          borderRadius: 7, fontSize: 12, fontWeight: 700,
          cursor: busy ? "wait" : "pointer",
          opacity: busy ? 0.85 : 1,
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          transition: "background 0.2s ease, opacity 0.15s",
        }}>
        {icon}
        <span>{label}</span>
      </button>
      {phase === "error" && errorMsg && (
        <div style={{
          marginTop: 6, fontSize: 11, color: "var(--lm-red, #ef4444)",
          textAlign: "center",
        }}>{errorMsg}</div>
      )}
    </>
  );
}

const labelStyle = {
  display: "block" as const,
  fontSize: 11, color: C.textDim, marginBottom: 5,
};
const selectStyle = {
  width: "100%", boxSizing: "border-box" as const,
  background: C.surface, border: `1px solid ${C.border}`,
  borderRadius: 7, padding: "8px 11px",
  fontSize: 12.5, color: C.text, outline: "none", cursor: "pointer",
};
