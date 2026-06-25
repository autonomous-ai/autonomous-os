import { useEffect, useRef, useState, useCallback } from "react";
import { toast } from "sonner";
import { getDeviceConfig, updateDeviceConfig, getTTSVoices, getTTSProviders } from "@/lib/api";
import type { DeviceConfig } from "@/lib/api";
import type { ChannelType } from "@/types";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";
import { C, ADMIN_PASSWORD_MIN } from "@/components/setup/shared";
import { DeviceSection } from "@/components/setup/DeviceSection";
import { LLMSection } from "@/components/setup/LLMSection";
import { WifiSection } from "@/pages/settings/WifiSection";
import { VoiceSection as EditVoiceSection } from "@/pages/settings/VoiceSection";
import { FaceSection as EditFaceSection } from "@/pages/settings/FaceSection";
import { TTSSection } from "@/pages/settings/TTSSection";
import { RealtimeSection } from "@/pages/settings/RealtimeSection";
import { AgentRuntimeSection } from "@/pages/settings/AgentRuntimeSection";
import { STTSection, type SttProvider } from "@/pages/settings/STTSection";
import { ChannelSection } from "@/pages/settings/ChannelSection";
import { MqttSection } from "@/pages/settings/MqttSection";

// The set of sections this panel can render. Controlled by the parent now (the
// page shell owns the sidebar / active-section state). `stt` is the Language
// section (rendered under id="stt"), matching the legacy /edit layout. `runtime`
// is the agent-backend switch (its own Switch button, not part of Save).
export type SettingsSectionId = "device" | "wifi" | "llm" | "runtime" | "voice" | "face" | "tts" | "realtime" | "stt" | "channel" | "mqtt";

// Header-row label lookup. Kept local so the panel can render the active-section
// title above the form without depending on the page's NAV_GROUPS config.
const SECTION_LABELS: Record<SettingsSectionId, string> = {
  device: "General",
  wifi: "Wi-Fi",
  llm: "AI Brain",
  runtime: "Runtime",
  voice: "My Voice",
  face: "Face",
  tts: "Voice",
  realtime: "Realtime",
  stt: "Language",
  channel: "Channels",
  mqtt: "MQTT",
};

// Field / LockedField / LockedPasswordField / SectionCard live in
// @/components/setup/shared. SkeletonBlock stays inline because this panel's
// version renders 4 stacked cards whereas Setup's renders just one.

function SkeletonBlock() {
  const bar = (w: string | number, h = 10) => (
    <div style={{ width: w, height: h, borderRadius: 6, background: C.surface, marginBottom: 10 }} />
  );
  return (
    <>
      {[1, 2, 3, 4].map((i) => (
        <div key={i} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: "18px 20px", marginBottom: 16 }}>
          {bar(80, 8)}
          <div style={{ marginTop: 14 }}>{bar("100%", 32)}{bar("100%", 32)}</div>
        </div>
      ))}
    </>
  );
}

// SettingsPanel — the self-contained settings form body. Owns all form state,
// config load/save, and the section components. Renders WITHOUT a sidebar so it
// can be embedded both inside the /edit page shell and inside the Monitor
// dashboard. `activeSection` is controlled by the parent.
export function SettingsPanel({ activeSection }: { activeSection: SettingsSectionId }): React.JSX.Element {
  const [loadingCfg, setLoadingCfg] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // form state
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  // Rotate-admin-password field. Empty = no change; non-empty = bcrypt +
  // replace server-side. Existing session cookie keeps working since it's
  // signed by SessionSecret, not by the password hash.
  const [adminPassword, setAdminPassword] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [mac, setMac] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmUrl, setLlmUrl] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmDisableThinking, setLlmDisableThinking] = useState(false);
  const [deepgramApiKey, setDeepgramApiKey] = useState("");
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
  // STT provider: derived from saved config (deepgram if key present, else autonomous).
  // Default for fresh devices is "autonomous" — uses LLM endpoint as fallback.
  const [sttProvider, setSttProvider] = useState<SttProvider>("autonomous");
  // STT language drives model selection on the server (operators don't pick
  // model directly). Defaults to "en" so a never-configured device lands on
  // English instead of "auto/unset" (which surfaces as a blank dropdown).
  const [sttLanguage, setSttLanguage] = useState("en");
  const [ttsApiKey, setTtsApiKey] = useState("");
  const [ttsBaseUrl, setTtsBaseUrl] = useState("");
  const [ttsProvider, setTtsProvider] = useState("openai");
  const [ttsProviders, setTtsProviders] = useState<string[]>([]);
  const [ttsVoice, setTtsVoice] = useState("alloy");
  const [ttsVoices, setTtsVoices] = useState<string[]>([]);
  const [realtimeEnabled, setRealtimeEnabled] = useState(true);
  const [realtimeProvider, setRealtimeProvider] = useState("gemini");
  const [realtimeVoice, setRealtimeVoice] = useState("Kore");
  const [realtimeReasoning, setRealtimeReasoning] = useState("MINIMAL");
  const [realtimeApiKey, setRealtimeApiKey] = useState("");
  const [realtimeBaseUrl, setRealtimeBaseUrl] = useState("");
  const [channel, setChannel] = useState<ChannelType>("telegram");
  const [teleToken, setTeleToken] = useState("");
  const [teleUserId, setTeleUserId] = useState("");
  const [slackBotToken, setSlackBotToken] = useState("");
  const [slackAppToken, setSlackAppToken] = useState("");
  const [slackUserId, setSlackUserId] = useState("");
  const [discordBotToken, setDiscordBotToken] = useState("");
  const [discordGuildId, setDiscordGuildId] = useState("");
  const [discordUserId, setDiscordUserId] = useState("");
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");
  // Snapshot of MQTT fields that were already populated when config loaded.
  // Locks those fields against edits; fields blank at load remain editable.
  const [mqttLoaded, setMqttLoaded] = useState({
    endpoint: false, port: false, username: false,
    password: false, faChannel: false, fdChannel: false,
  });
  // Same idea for messaging-channel credentials. Already-saved values render
  // read-only with an inline "Edit" button to opt-in to changing them.
  const [channelLoaded, setChannelLoaded] = useState({
    teleToken: false, teleUserId: false,
    slackBotToken: false, slackAppToken: false, slackUserId: false,
    discordBotToken: false, discordGuildId: false, discordUserId: false,
  });
  const [wifiLoaded, setWifiLoaded] = useState({ ssid: false, password: false });
  const [llmLoaded, setLlmLoaded] = useState({ apiKey: false, baseUrl: false, model: false });
  const [ttsLoaded, setTtsLoaded] = useState({ apiKey: false, baseUrl: false });
  const [realtimeLoaded, setRealtimeLoaded] = useState({ apiKey: false });
  const [sttLoaded, setSttLoaded] = useState({ deepgram: false, apiKey: false, baseUrl: false });

  // Baseline snapshot of non-secret fields captured after load (and after every
  // successful save). Used to gate Save button on dirty-only. Secrets are
  // handled separately: their input state is empty when nothing was typed, so
  // any non-empty secret state implies a pending change.
  type InitialSnapshot = {
    ssid: string; deviceId: string;
    llmUrl: string; llmModel: string; llmDisableThinking: boolean;
    sttBaseUrl: string; sttProvider: SttProvider; sttLanguage: string;
    ttsBaseUrl: string; ttsProvider: string; ttsVoice: string;
    channel: ChannelType;
    teleUserId: string; slackUserId: string;
    discordGuildId: string; discordUserId: string;
    mqttEndpoint: string; mqttPort: string; mqttUsername: string;
    faChannel: string; fdChannel: string;
  };
  const initialRef = useRef<InitialSnapshot | null>(null);

  // Face owners — top-level state because both Voice and Face sections read
  // it. Section-local state (faceName, voiceLabel, etc.) lives in the section
  // components themselves.
  const [faceOwners, setFaceOwners] = useState<FaceOwner[]>([]);

  const loadFaceOwners = useCallback(async () => {
    try {
      const r = await fetch("/hw/face/owners").then((x) => x.json());
      if (Array.isArray(r?.persons)) setFaceOwners(r.persons);
    } catch {}
  }, []);

  useEffect(() => { loadFaceOwners(); }, [loadFaceOwners]);

  useEffect(() => {
    getDeviceConfig()
      .then((cfg: DeviceConfig) => {
        // ConfigPublicResponse — secrets are returned as has_* booleans only.
        // State for secret fields stays empty until the operator types a new
        // value in SecretUpdateField; submit then ships only the touched ones.
        setSsid(cfg.network_ssid ?? "");
        setDeviceId(cfg.device_id ?? "");
        setMac(cfg.mac ?? "");
        setLlmUrl(cfg.llm_base_url ?? "");
        setLlmModel(cfg.llm_model ?? "");
        setLlmDisableThinking(cfg.llm_disable_thinking ?? false);
        setSttBaseUrl(cfg.stt_base_url ?? "");
        setSttProvider(cfg.has_deepgram_api_key ? "deepgram" : "autonomous");
        setSttLanguage(cfg.stt_language || "en");
        setTtsBaseUrl(cfg.tts_base_url ?? "");
        setTtsProvider(cfg.tts_provider || "openai");
        setTtsVoice(cfg.tts_voice || "alloy");
        if (cfg.realtime) {
          setRealtimeEnabled(cfg.realtime.enabled ?? true);
          setRealtimeProvider(cfg.realtime.provider || "gemini");
          if (cfg.realtime.voice) setRealtimeVoice(cfg.realtime.voice);
          if (cfg.realtime.reasoning) setRealtimeReasoning(cfg.realtime.reasoning);
          setRealtimeBaseUrl(cfg.realtime.base_url ?? "");
          setRealtimeLoaded({ apiKey: !!cfg.realtime.has_api_key });
        }
        setChannel((cfg.channel as ChannelType) || "telegram");
        setTeleUserId(cfg.telegram_user_id ?? "");
        setSlackUserId(cfg.slack_user_id ?? "");
        setDiscordGuildId(cfg.discord_guild_id ?? "");
        setDiscordUserId(cfg.discord_user_id ?? "");
        setMqttEndpoint(cfg.mqtt_endpoint ?? "");
        setMqttPort(cfg.mqtt_port ? String(cfg.mqtt_port) : "");
        setMqttUsername(cfg.mqtt_username ?? "");
        setFaChannel(cfg.fa_channel ?? "");
        setFdChannel(cfg.fd_channel ?? "");
        setMqttLoaded({
          endpoint: !!cfg.mqtt_endpoint,
          port: !!cfg.mqtt_port,
          username: !!cfg.mqtt_username,
          password: cfg.has_mqtt_password,
          faChannel: !!cfg.fa_channel,
          fdChannel: !!cfg.fd_channel,
        });
        setChannelLoaded({
          teleToken: cfg.has_telegram_bot_token,
          teleUserId: !!cfg.telegram_user_id,
          slackBotToken: cfg.has_slack_bot_token,
          slackAppToken: cfg.has_slack_app_token,
          slackUserId: !!cfg.slack_user_id,
          discordBotToken: cfg.has_discord_bot_token,
          discordGuildId: !!cfg.discord_guild_id,
          discordUserId: !!cfg.discord_user_id,
        });
        setWifiLoaded({
          ssid: !!cfg.network_ssid,
          password: cfg.has_network_password,
        });
        setLlmLoaded({
          apiKey: cfg.has_llm_api_key,
          baseUrl: !!cfg.llm_base_url,
          model: !!cfg.llm_model,
        });
        setTtsLoaded({
          apiKey: cfg.has_tts_api_key,
          baseUrl: !!cfg.tts_base_url,
        });
        setSttLoaded({
          deepgram: cfg.has_deepgram_api_key,
          apiKey: cfg.has_stt_api_key,
          baseUrl: !!cfg.stt_base_url,
        });
        // Mirror the post-load behavior of the LLM→TTS/STT base-URL auto-fill
        // effects so the baseline matches the rendered state. Without this,
        // a config with llm_base_url but no tts/stt_base_url would show the
        // form as dirty immediately on load.
        const llmUrlInit = cfg.llm_base_url ?? "";
        const sttProviderInit: SttProvider = cfg.has_deepgram_api_key ? "deepgram" : "autonomous";
        initialRef.current = {
          ssid: cfg.network_ssid ?? "",
          deviceId: cfg.device_id ?? "",
          llmUrl: llmUrlInit,
          llmModel: cfg.llm_model ?? "",
          llmDisableThinking: cfg.llm_disable_thinking ?? false,
          sttBaseUrl: (cfg.stt_base_url ?? "") || (sttProviderInit === "autonomous" ? llmUrlInit : ""),
          sttProvider: sttProviderInit,
          sttLanguage: cfg.stt_language || "en",
          ttsBaseUrl: (cfg.tts_base_url ?? "") || llmUrlInit,
          ttsProvider: cfg.tts_provider || "openai",
          ttsVoice: cfg.tts_voice || "alloy",
          channel: (cfg.channel as ChannelType) || "telegram",
          teleUserId: cfg.telegram_user_id ?? "",
          slackUserId: cfg.slack_user_id ?? "",
          discordGuildId: cfg.discord_guild_id ?? "",
          discordUserId: cfg.discord_user_id ?? "",
          mqttEndpoint: cfg.mqtt_endpoint ?? "",
          mqttPort: cfg.mqtt_port ? String(cfg.mqtt_port) : "",
          mqttUsername: cfg.mqtt_username ?? "",
          faChannel: cfg.fa_channel ?? "",
          fdChannel: cfg.fd_channel ?? "",
        };
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoadingCfg(false));
    getTTSProviders().then(setTtsProviders).catch(() => {});
    getTTSVoices().then(setTtsVoices).catch(() => {});
  }, []);

  // Refetch voices when provider OR stt_language changes — only reset voice
  // if the currently-saved voice is not in the new (filtered) list.
  // Passing sttLanguage filters ElevenLabs voices to the active language's
  // bucket so VN/CN owners only see voices that sound natural for them.
  const providerChangedByUser = useRef(false);
  useEffect(() => {
    getTTSVoices(ttsProvider, sttLanguage).then((voices) => {
      setTtsVoices(voices);
      if (providerChangedByUser.current && voices.length > 0 && !voices.includes(ttsVoice)) {
        setTtsVoice(voices[0]);
      }
      providerChangedByUser.current = true;
    }).catch(() => {});
  }, [ttsProvider, sttLanguage]);

  // Auto-mirror AI Brain key/URL into TTS while TTS field is empty.
  // Once the user types into TTS the sync stops; clearing it re-enables mirroring.
  useEffect(() => {
    if (!ttsApiKey && llmApiKey) setTtsApiKey(llmApiKey);
  }, [llmApiKey, ttsApiKey]);
  useEffect(() => {
    if (!ttsBaseUrl && llmUrl) setTtsBaseUrl(llmUrl);
  }, [llmUrl, ttsBaseUrl]);
  // Same auto-mirror for STT in autonomous mode (Deepgram has its own key).
  useEffect(() => {
    if (sttProvider === "autonomous" && !sttApiKey && llmApiKey) setSttApiKey(llmApiKey);
  }, [llmApiKey, sttApiKey, sttProvider]);
  useEffect(() => {
    if (sttProvider === "autonomous" && !sttBaseUrl && llmUrl) setSttBaseUrl(llmUrl);
  }, [llmUrl, sttBaseUrl, sttProvider]);

  // Dirty = any non-secret field diverges from the loaded/last-saved baseline,
  // OR any secret field has user-typed content. Save button uses this to stay
  // disabled until something actually changed.
  const baseline = initialRef.current;
  const dirty = !loadingCfg && baseline != null && (
    ssid !== baseline.ssid ||
    deviceId !== baseline.deviceId ||
    llmUrl !== baseline.llmUrl ||
    llmModel !== baseline.llmModel ||
    llmDisableThinking !== baseline.llmDisableThinking ||
    sttBaseUrl !== baseline.sttBaseUrl ||
    sttProvider !== baseline.sttProvider ||
    sttLanguage !== baseline.sttLanguage ||
    ttsBaseUrl !== baseline.ttsBaseUrl ||
    ttsProvider !== baseline.ttsProvider ||
    ttsVoice !== baseline.ttsVoice ||
    channel !== baseline.channel ||
    teleUserId !== baseline.teleUserId ||
    slackUserId !== baseline.slackUserId ||
    discordGuildId !== baseline.discordGuildId ||
    discordUserId !== baseline.discordUserId ||
    mqttEndpoint !== baseline.mqttEndpoint ||
    mqttPort !== baseline.mqttPort ||
    mqttUsername !== baseline.mqttUsername ||
    faChannel !== baseline.faChannel ||
    fdChannel !== baseline.fdChannel ||
    !!password || !!adminPassword || !!llmApiKey || !!ttsApiKey ||
    !!sttApiKey || !!deepgramApiKey || !!mqttPassword ||
    !!teleToken || !!slackBotToken || !!slackAppToken || !!discordBotToken
  );

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Admin password rotation is optional here (empty = keep current). But when
    // the operator IS rotating, hold them to the same ADMIN_PASSWORD_MIN floor as
    // initial setup so /edit can't be used to weaken the admin login below the
    // policy the setup flow enforces. Backend has no min-length, so this is the gate.
    if (adminPassword && adminPassword.length < ADMIN_PASSWORD_MIN) {
      setError(`New admin password must be at least ${ADMIN_PASSWORD_MIN} characters.`);
      return;
    }
    setSaving(true);
    try {
      // Build the payload from non-secret fields first, then layer on each
      // secret only when the operator typed something into its
      // SecretUpdateField. Empty secrets would otherwise clobber the saved
      // value on disk (PUT treats blanks as intentional clears for STT /
      // Deepgram). Channel id fields (telegram_user_id, slack_user_id,
      // discord_guild_id, discord_user_id) are non-secret and ship every time.
      const body: Record<string, unknown> = {
        ssid: ssid.trim(),
        channel,
        llm_base_url: llmUrl, llm_model: llmModel,
        llm_disable_thinking: llmDisableThinking,
        stt_base_url: sttBaseUrl, stt_language: sttLanguage,
        tts_base_url: ttsBaseUrl, tts_provider: ttsProvider, tts_voice: ttsVoice,
        device_id: deviceId,
        mqtt_endpoint: mqttEndpoint, mqtt_username: mqttUsername,
        mqtt_port: mqttPort ? parseInt(mqttPort, 10) : 0,
        fa_channel: faChannel, fd_channel: fdChannel,
      };
      if (password) body.password = password;
      if (adminPassword) body.admin_password = adminPassword;
      // Realtime block — server applies + restarts hal. api_key only when typed.
      const realtime: Record<string, unknown> = { enabled: realtimeEnabled, provider: realtimeProvider };
      if (realtimeProvider !== "none") { realtime.voice = realtimeVoice; realtime.reasoning = realtimeReasoning; }
      if (realtimeBaseUrl) realtime.base_url = realtimeBaseUrl;
      if (realtimeApiKey) realtime.api_key = realtimeApiKey;
      body.realtime = realtime;
      if (llmApiKey) body.llm_api_key = llmApiKey;
      if (ttsApiKey) body.tts_api_key = ttsApiKey;
      if (mqttPassword) body.mqtt_password = mqttPassword;
      // STT provider switch: clear the opposing key explicitly so the
      // operator's mode toggle takes effect. When staying on the same provider
      // and not typing a new key, leave both fields untouched.
      if (sttProvider === "deepgram") {
        if (deepgramApiKey) body.deepgram_api_key = deepgramApiKey;
        if (sttLoaded.apiKey || sttApiKey) body.stt_api_key = "";
      } else {
        if (sttApiKey) body.stt_api_key = sttApiKey;
        if (sttLoaded.deepgram || deepgramApiKey) body.deepgram_api_key = "";
      }
      // Channel credentials: send IDs always, tokens only when typed.
      if (channel === "telegram") {
        body.telegram_user_id = teleUserId;
        if (teleToken) body.telegram_bot_token = teleToken;
      } else if (channel === "slack") {
        body.slack_user_id = slackUserId;
        if (slackBotToken) body.slack_bot_token = slackBotToken;
        if (slackAppToken) body.slack_app_token = slackAppToken;
      } else {
        body.discord_guild_id = discordGuildId;
        body.discord_user_id = discordUserId;
        if (discordBotToken) body.discord_bot_token = discordBotToken;
      }
      await updateDeviceConfig(body);
      toast.success("Config saved — restart your device for changes to take effect.");
      // Reset baseline so Save button goes back to disabled until next edit.
      // Non-secret fields adopt their current values as the new baseline.
      initialRef.current = {
        ssid, deviceId,
        llmUrl, llmModel, llmDisableThinking,
        sttBaseUrl, sttProvider, sttLanguage,
        ttsBaseUrl, ttsProvider, ttsVoice,
        channel,
        teleUserId, slackUserId,
        discordGuildId, discordUserId,
        mqttEndpoint, mqttPort, mqttUsername,
        faChannel, fdChannel,
      };
      // Clear typed secrets so their non-empty state no longer marks the form
      // dirty. Their persisted values live server-side; has_* flags surface
      // "configured" in the UI.
      setPassword(""); setAdminPassword("");
      setLlmApiKey(""); setTtsApiKey(""); setSttApiKey("");
      setDeepgramApiKey(""); setMqttPassword("");
      setTeleToken(""); setSlackBotToken(""); setSlackAppToken("");
      setDiscordBotToken("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    }
    setSaving(false);
  }, [
    channel, teleToken, teleUserId, slackBotToken, slackAppToken, slackUserId,
    discordBotToken, discordGuildId, discordUserId, ssid, password, adminPassword, llmUrl,
    llmApiKey, llmModel, llmDisableThinking, deepgramApiKey, sttApiKey, sttBaseUrl,
    sttProvider, sttLanguage, sttLoaded,
    ttsApiKey, ttsBaseUrl, ttsProvider, ttsVoice, deviceId,
    mqttEndpoint, mqttUsername, mqttPassword, mqttPort, faChannel, fdChannel,
    realtimeEnabled, realtimeProvider, realtimeVoice, realtimeReasoning, realtimeApiKey, realtimeBaseUrl,
  ]);

  // Save is hidden for sections that aren't part of the form's PUT flow: Face/My
  // Voice enroll via their own buttons, and Runtime switches via its own action.
  const showSave = activeSection !== "face" && activeSection !== "voice" && activeSection !== "runtime";

  return (
    <div className="lm-fade-in lm-settings-panel" style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px" }}>
      <div style={{ maxWidth: 560, margin: "0 auto" }}>

        {/* Header row: active-section label on the left, Save button on the right.
            A hairline divider under the row separates the title from the body. */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          marginBottom: 18, paddingBottom: 14, borderBottom: `1px solid ${C.border}`,
        }}>
          <span className="lm-settings-title" style={{ fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em" }}>
            {SECTION_LABELS[activeSection]}
          </span>
          {showSave && (
            <button
              form="edit-form"
              type="submit"
              disabled={saving || loadingCfg || !dirty}
              style={{
                padding: "6px 18px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                cursor: saving || loadingCfg || !dirty ? "not-allowed" : "pointer",
                border: "none",
                background: saving || loadingCfg || !dirty ? C.surface : C.amber,
                // Dark ink that reads on the amber fill in both themes; theme-
                // constant on purpose (see --lm-on-amber in index.css).
                color: saving || loadingCfg || !dirty ? C.textMuted : "var(--lm-on-amber)",
                transition: "all 0.15s",
                opacity: saving || loadingCfg || !dirty ? 0.6 : 1,
              }}
            >
              {saving ? "Saving…" : "Save Changes"}
            </button>
          )}
        </div>

        {error && (
          <div style={{
            background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
            borderRadius: 8, padding: "10px 14px", fontSize: 12, color: C.red, marginBottom: 16,
          }}>
            {error}
          </div>
        )}

        {loadingCfg ? <SkeletonBlock /> : (
          <form id="edit-form" onSubmit={handleSubmit}>

            <DeviceSection
              active={activeSection === "device"}
              deviceId={deviceId} setDeviceId={setDeviceId}
              mac={mac}
              rotateAdminPassword={adminPassword}
              setRotateAdminPassword={setAdminPassword}
            />

            <WifiSection
              active={activeSection === "wifi"}
              wifiLoaded={wifiLoaded}
              ssid={ssid} setSsid={setSsid}
              password={password} setPassword={setPassword}
            />

            <LLMSection
              active={activeSection === "llm"}
              llmLoaded={llmLoaded}
              llmApiKey={llmApiKey} setLlmApiKey={setLlmApiKey}
              llmUrl={llmUrl} setLlmUrl={setLlmUrl}
              llmModel={llmModel} setLlmModel={setLlmModel}
            />

            <AgentRuntimeSection active={activeSection === "runtime"} />

            <EditVoiceSection
              active={activeSection === "voice"}
              sttLanguage={sttLanguage}
              faceOwners={faceOwners}
              loadFaceOwners={loadFaceOwners}
            />

            <EditFaceSection
              active={activeSection === "face"}
              faceOwners={faceOwners}
              loadFaceOwners={loadFaceOwners}
            />

            <TTSSection
              active={activeSection === "tts"}
              ttsLoaded={ttsLoaded}
              llmLoaded={llmLoaded}
              ttsApiKey={ttsApiKey} setTtsApiKey={setTtsApiKey}
              ttsBaseUrl={ttsBaseUrl} setTtsBaseUrl={setTtsBaseUrl}
              ttsProvider={ttsProvider} setTtsProvider={setTtsProvider}
              ttsProviders={ttsProviders}
              ttsVoice={ttsVoice} setTtsVoice={setTtsVoice}
              ttsVoices={ttsVoices}
              sttLanguage={sttLanguage}
            />

            <RealtimeSection
              active={activeSection === "realtime"}
              realtimeLoaded={realtimeLoaded}
              llmLoaded={llmLoaded}
              enabled={realtimeEnabled} setEnabled={setRealtimeEnabled}
              provider={realtimeProvider} setProvider={setRealtimeProvider}
              voice={realtimeVoice} setVoice={setRealtimeVoice}
              reasoning={realtimeReasoning} setReasoning={setRealtimeReasoning}
              apiKey={realtimeApiKey} setApiKey={setRealtimeApiKey}
              baseUrl={realtimeBaseUrl} setBaseUrl={setRealtimeBaseUrl}
            />

            <STTSection
              active={activeSection === "stt"}
              sttLanguage={sttLanguage} setSttLanguage={setSttLanguage}
              sttProvider={sttProvider} setSttProvider={setSttProvider}
              sttLoaded={sttLoaded}
              llmLoaded={llmLoaded}
              deepgramApiKey={deepgramApiKey} setDeepgramApiKey={setDeepgramApiKey}
              sttApiKey={sttApiKey} setSttApiKey={setSttApiKey}
              sttBaseUrl={sttBaseUrl} setSttBaseUrl={setSttBaseUrl}
            />

            <ChannelSection
              active={activeSection === "channel"}
              channel={channel} setChannel={setChannel}
              channelLoaded={channelLoaded}
              teleToken={teleToken} setTeleToken={setTeleToken}
              teleUserId={teleUserId} setTeleUserId={setTeleUserId}
              slackBotToken={slackBotToken} setSlackBotToken={setSlackBotToken}
              slackAppToken={slackAppToken} setSlackAppToken={setSlackAppToken}
              slackUserId={slackUserId} setSlackUserId={setSlackUserId}
              discordBotToken={discordBotToken} setDiscordBotToken={setDiscordBotToken}
              discordGuildId={discordGuildId} setDiscordGuildId={setDiscordGuildId}
              discordUserId={discordUserId} setDiscordUserId={setDiscordUserId}
            />

            <MqttSection
              active={activeSection === "mqtt"}
              mqttLoaded={mqttLoaded}
              mqttEndpoint={mqttEndpoint} setMqttEndpoint={setMqttEndpoint}
              mqttPort={mqttPort} setMqttPort={setMqttPort}
              mqttUsername={mqttUsername} setMqttUsername={setMqttUsername}
              mqttPassword={mqttPassword} setMqttPassword={setMqttPassword}
              faChannel={faChannel} setFaChannel={setFaChannel}
              fdChannel={fdChannel} setFdChannel={setFdChannel}
            />

          </form>
        )}
      </div>
    </div>
  );
}
