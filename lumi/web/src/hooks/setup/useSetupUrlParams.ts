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
}

// Snapshot the URL query string at module-load time, BEFORE React renders
// and BEFORE App.tsx's useScrubSecrets() effect strips secrets via
// window.history.replaceState. Setup mounts lazily: SetupGate renders null
// while it probes the device, and by the time Setup actually mounts the
// scrub has already wiped llm_api_key / tele_token / etc. from
// window.location.search — reading via useSearchParams() at that point
// returns empty strings. The module-level snapshot captures the operator-
// provided values up front so the form state still ships them on submit.
const INITIAL_SEARCH: string =
  typeof window !== "undefined" ? window.location.search : "";
const INITIAL_PARAMS: URLSearchParams = new URLSearchParams(INITIAL_SEARCH);

// The raw original query string ("?…") — used to build cross-origin redirect
// URLs that must carry every param through the AP→.local handoff (so the new
// origin can re-auth via the bearer in `llm_api_key` and prefill state from
// the lumi-pushed values). Reading window.location.search at redirect time
// returns the post-scrub value with secrets stripped — useless for the
// re-auth step.
export function getInitialSearch(): string {
  return INITIAL_SEARCH;
}

// searchParams is no longer read inside the hook (kept in the signature so
// the call site doesn't need to change), but the dep array intentionally
// excludes it: the snapshot is fixed for the session.
export function useSetupUrlParams(_searchParams: URLSearchParams): SetupUrlParams {
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
    }),
    [],
  );
}
