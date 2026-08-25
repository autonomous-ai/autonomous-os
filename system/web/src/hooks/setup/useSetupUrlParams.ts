import { useMemo } from "react";

export interface SetupUrlParams {
  teleToken: string;
  teleUserId: string;
  slackBotToken: string;
  slackAppToken: string;
  slackUserId: string;
  discordBotToken: string;
  discordGuildId: string;
  discordUserId: string;
  llmApiKey: string;
  llmUrl: string;
  llmModel: string;
  deepgramApiKey: string;
  ttsApiKey: string;
  ttsBaseUrl: string;
  deviceId: string;
  mqttEndpoint: string;
  mqttPort: string;
  mqttUsername: string;
  mqttPassword: string;
  faChannel: string;
  fdChannel: string;
  sttLanguage: string;
  ttsProvider: string;
  ttsVoice: string;
  ttsModel: string;
}

// Snapshot the URL query string at module-load time, BEFORE React renders
// and BEFORE App.tsx's useScrubSecrets() effect strips secrets via
// window.history.replaceState. Setup mounts lazily: SetupGate renders null
// while it probes the device, and by the time Setup actually mounts the
// scrub has already wiped llm_api_key / tele_token / etc. from
// window.location.search — reading via useSearchParams() at that point
// returns empty strings. The module-level snapshot captures the operator-
// provided values up front so the form state still ships them on submit.
//
// F5-reload survival: the scrub uses history.replaceState, which changes the
// URL the browser reloads. On reload, window.location.search is already
// empty — the module snapshot would be lost. Persist the raw search string
// to sessionStorage (per-tab, cleared on tab close) so a reload during the
// same setup session re-hydrates the params from there. sessionStorage is
// SAFER than URL as a resting place: not shown in the address bar, not
// captured by screenshots of the URL, not walked by "back" history.
// Key MUST match SETUP_URL_SEARCH_STORE_KEY in lib/api.ts (scrubLocationSecrets
// writes here before wiping the URL; this module reads from here on reload).
const SESSION_STORE_KEY = "autonomous.setup_url_search.v1";
const INITIAL_SEARCH: string = (() => {
  if (typeof window === "undefined") return "";
  const fromURL = window.location.search;
  // A non-empty URL wins — a fresh open from the companion app is authoritative
  // and should overwrite whatever a prior session persisted.
  if (fromURL) {
    try { sessionStorage.setItem(SESSION_STORE_KEY, fromURL); } catch {
      /* private-mode / disabled storage — fall back to URL-only behavior */
    }
    return fromURL;
  }
  // Empty URL (post-scrub reload): try sessionStorage rehydration.
  try {
    return sessionStorage.getItem(SESSION_STORE_KEY) ?? "";
  } catch {
    return "";
  }
})();
const INITIAL_PARAMS: URLSearchParams = new URLSearchParams(INITIAL_SEARCH);

// The raw original query string ("?…") — used to build cross-origin redirect
// URLs that must carry every param through the AP→.local handoff (so the new
// origin can re-auth via the bearer in `llm_api_key` and prefill state from
// the OS-server-pushed values). Reading window.location.search at redirect time
// returns the post-scrub value with secrets stripped — useless for the
// re-auth step.
export function getInitialSearch(): string {
  return INITIAL_SEARCH;
}

// Wipes every trace of the current Setup attempt and hard-reloads into a
// pristine wizard — the state a device shows the very first time an operator
// joins its AP. Used by the Wi-Fi-failure recovery path (see
// SetupProgressScreen's "Start over" action).
//
// A hard reload is REQUIRED, not a convenience: INITIAL_SEARCH / INITIAL_PARAMS
// above are module-level constants frozen at script-load time. Resetting React
// state alone would leave the old operator-supplied params (llm_api_key,
// device_id, …) live in this module, so the "clean" form would still submit
// stale credentials. Only a fresh document re-evaluates them.
//
// Cleared, in order:
//   1. sessionStorage params snapshot — otherwise the reload rehydrates the
//      exact query string we're trying to discard (see INITIAL_SEARCH above).
//   2. The URL's query + hash — the reload must land on a bare /setup, since a
//      surviving `?llm_api_key=…` would repopulate the snapshot in step 1.
// Deliberately NOT cleared: the theme preference (localStorage `theme`), which
// is a device-wide user choice unrelated to this setup attempt.
export function resetSetupSession(): void {
  if (typeof window === "undefined") return;
  clearStoredSetupParams();
  // replace() (not assign) so the dirty URL doesn't linger in history where a
  // Back press would restore the params we just discarded.
  window.location.replace(`${window.location.pathname}`);
}

// Drops the persisted params snapshot WITHOUT reloading.
//
// Used on the failed-join recovery path, where the page has already mounted and
// we want the next reload — however it happens (F5, the operator reopening the
// popup, the companion app pushing a fresh URL) — to come up with nothing
// carried over from the attempt that failed.
//
// Why this is needed separately from resetSetupSession(): sessionStorage is
// per-tab but survives F5. An operator who leaves the tab open, rejoins the
// device AP and presses reload would otherwise rehydrate the exact query string
// from the failed attempt (see INITIAL_SEARCH above) — a form that looks clean
// but still ships the old llm_api_key. Clearing at mount closes that window
// while leaving the current document's already-frozen INITIAL_PARAMS untouched,
// so the in-place "Back to Wi-Fi" retry still has what it needs to submit.
export function clearStoredSetupParams(): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.removeItem(SESSION_STORE_KEY);
  } catch {
    /* private-mode / disabled storage — nothing was persisted to begin with */
  }
}

// Takes no arguments: the params come from INITIAL_SEARCH, the module-load
// snapshot taken before App's scrub strips secrets from the URL (see above).
// Live `searchParams` would be the post-scrub value — empty for exactly the
// fields callers need — so the hook deliberately ignores the router's copy and
// the signature no longer accepts one.
export function useSetupUrlParams(): SetupUrlParams {
  return useMemo(
    () => ({
      teleToken: INITIAL_PARAMS.get("tele_token") ?? "",
      teleUserId: INITIAL_PARAMS.get("tele_user_id") ?? "",
      slackBotToken: INITIAL_PARAMS.get("slack_bot_token") ?? "",
      slackAppToken: INITIAL_PARAMS.get("slack_app_token") ?? "",
      slackUserId: INITIAL_PARAMS.get("slack_user_id") ?? "",
      discordBotToken: INITIAL_PARAMS.get("discord_bot_token") ?? "",
      discordGuildId: INITIAL_PARAMS.get("discord_guild_id") ?? "",
      discordUserId: INITIAL_PARAMS.get("discord_user_id") ?? "",
      llmApiKey: INITIAL_PARAMS.get("llm_api_key") ?? "",
      llmUrl: INITIAL_PARAMS.get("llm_url") ?? "",
      llmModel: INITIAL_PARAMS.get("llm_model") ?? "",
      deepgramApiKey: INITIAL_PARAMS.get("deepgram_api_key") ?? "",
      ttsApiKey: INITIAL_PARAMS.get("tts_api_key") ?? "",
      ttsBaseUrl: INITIAL_PARAMS.get("tts_base_url") ?? "",
      deviceId: INITIAL_PARAMS.get("device_id") ?? "",
      mqttEndpoint: INITIAL_PARAMS.get("mqtt_endpoint") ?? "",
      mqttPort: INITIAL_PARAMS.get("mqtt_port") ?? "",
      mqttUsername: INITIAL_PARAMS.get("mqtt_username") ?? "",
      mqttPassword: INITIAL_PARAMS.get("mqtt_password") ?? "",
      faChannel: INITIAL_PARAMS.get("fa_channel") ?? "",
      fdChannel: INITIAL_PARAMS.get("fd_channel") ?? "",
      sttLanguage: INITIAL_PARAMS.get("stt_language") ?? "",
      ttsProvider: INITIAL_PARAMS.get("tts_provider") ?? "",
      ttsVoice: INITIAL_PARAMS.get("tts_voice") ?? "",
      ttsModel: INITIAL_PARAMS.get("tts_model") ?? "",
    }),
    [],
  );
}
