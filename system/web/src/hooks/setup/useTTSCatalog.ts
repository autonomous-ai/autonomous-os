import { useEffect, useRef, useState } from "react";
import { getTTSProviders, getTTSVoices } from "@/lib/api";

// Manages the TTS provider+voice dropdowns for Setup. Encapsulates the
// fetch-on-mount + refetch-on-provider/lang-change pattern plus the
// URL-prefill validation (server has no allow-list, so we gate FE-side).
export function useTTSCatalog({
  ttsProvider,
  sttLanguage,
  ttsVoice,
  ttsModel,
  urlProvider,
  urlVoice,
  setTtsProvider,
  setTtsVoice,
}: {
  ttsProvider: string;
  sttLanguage: string;
  ttsVoice: string;
  // Only consulted when ttsProvider === "openai": it selects which voice
  // list the server asks its configured TTS host for. The host itself is
  // never sent — /api/device/voices is unauthenticated and derives it from
  // the saved config, so mid-wizard (nothing saved yet) openai returns the
  // static list and the voice field stays free-typed.
  ttsModel: string;
  urlProvider: string;
  urlVoice: string;
  setTtsProvider: (v: string) => void;
  setTtsVoice: (v: string) => void;
}) {
  const [ttsProviders, setTtsProviders] = useState<string[]>([]);
  const [ttsVoices, setTtsVoices] = useState<string[]>([]);

  // Mount: load provider list + validate URL provider against allow-list.
  useEffect(() => {
    getTTSProviders().then((providers) => {
      setTtsProviders(providers);
      if (urlProvider && providers.length > 0 && !providers.includes(urlProvider)) {
        console.warn(`[setup] URL tts_provider="${urlProvider}" not in ${providers.join(",")}, using ${providers[0]}`);
        setTtsProvider(providers[0]);
      }
    }).catch(() => {});
    getTTSVoices().then(setTtsVoices).catch(() => {});
    // Intentional empty deps — mount-only, like the original effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refetch voices when provider, language, or (for openai) the model change
  // — only reset voice if the currently-selected one is not in the new
  // (filtered) list. Passing sttLanguage filters ElevenLabs voices to the
  // active language bucket. Debounced 500ms so typing into the (Setup debug)
  // model field doesn't fire a request per keystroke.
  const providerChangedByUser = useRef(false);
  const urlVoiceValidated = useRef(false);
  const openaiModel = ttsProvider === "openai" ? ttsModel : "";
  useEffect(() => {
    const timer = window.setTimeout(() => {
      getTTSVoices(ttsProvider, sttLanguage, openaiModel).then((voices) => {
        setTtsVoices(voices);
        if (voices.length > 0 && !voices.includes(ttsVoice)) {
          // Reset cases: (a) user switched provider/lang, voice no longer valid;
          // (b) first load and URL prefilled an invalid voice. Skip otherwise to
          // avoid clobbering a saved-cfg voice that's still loading in parallel.
          const urlVoiceInvalid = !urlVoiceValidated.current && !!urlVoice;
          if (providerChangedByUser.current || urlVoiceInvalid) {
            if (urlVoiceInvalid) {
              console.warn(`[setup] URL tts_voice="${urlVoice}" not in voice list for provider=${ttsProvider} lang=${sttLanguage || "auto"}, using ${voices[0]}`);
            }
            setTtsVoice(voices[0]);
          }
        }
        urlVoiceValidated.current = true;
        providerChangedByUser.current = true;
      }).catch(() => {});
    }, 500);
    return () => window.clearTimeout(timer);
    // Deliberately narrow deps: this effect must refetch ONLY when the
    // provider, language, or (openai) model change. `ttsVoice`/
    // `setTtsVoice` are what the effect writes, so adding them would refire
    // the catalog fetch on every voice selection (and re-run the reset logic
    // against a half-updated list); `urlVoice` is a one-shot URL prefill
    // read behind the `urlVoiceValidated` latch, so it must not drive
    // re-runs either.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ttsProvider, sttLanguage, openaiModel]);

  return { ttsProviders, ttsVoices };
}
