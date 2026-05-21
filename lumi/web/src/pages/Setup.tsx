import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { getNetworks, setupDevice } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useSetupUrlParams, getInitialSearch } from "@/hooks/setup/useSetupUrlParams";
import { useTTSCatalog } from "@/hooks/setup/useTTSCatalog";
import { useConfigPrefill } from "@/hooks/setup/useConfigPrefill";
import { useSetupStatusPolling } from "@/hooks/setup/useSetupStatusPolling";
import { useFaceEnroll } from "@/hooks/setup/useFaceEnroll";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "@/hooks/setup/types";
import { C } from "@/components/setup/shared";
import { DeviceSection } from "@/components/setup/DeviceSection";
import { WifiSection } from "@/components/setup/WifiSection";
import { LLMSection } from "@/components/setup/LLMSection";
import { ChannelSection } from "@/components/setup/ChannelSection";
import { LanguageSection } from "@/components/setup/LanguageSection";
import { TTSSection } from "@/components/setup/TTSSection";
import { VoiceSection } from "@/components/setup/VoiceSection";
import { FaceSection } from "@/components/setup/FaceSection";
import type { ChannelType, NetworkItem } from "@/types";
import { Wifi, Lamp, Brain, Volume2, MessageSquare, UserCircle, Mic, Globe, Check } from "lucide-react";

// SetupMode controls which sections render. Initial = AP/offline (hide
// online-only enrollments + tests), Continue = LAN/online (lamp can hit
// APIs, so Voice/Face enroll + TTS preview become available).
export type SetupMode = "initial" | "continue";

// Go playground/validator returns errors shaped like:
//   "Key: 'SetupRequest.SSID' Error:Field validation for 'SSID' failed on the
//    'required' tag\nKey: 'SetupRequest.LLMAPIKey' Error:Field validation …"
// Surface that as a human-readable list of missing fields so operators don't
// see what looks like a stack trace. Falls through unchanged when the message
// doesn't match the validator format (other backend errors stay as-is).
const FIELD_LABELS: Record<string, string> = {
  SSID: "Wi-Fi name",
  Password: "Wi-Fi password",
  LLMAPIKey: "AI Brain API key",
  LLMBaseURL: "AI Brain URL",
  DeviceID: "Device ID",
};
function normaliseSetupError(message: string): string {
  const matches = [...message.matchAll(/Field validation for '(\w+)' failed on the '(\w+)' tag/g)];
  if (matches.length === 0) return message;
  const missing: string[] = [];
  const other: string[] = [];
  for (const [, field, tag] of matches) {
    const label = FIELD_LABELS[field] ?? field;
    (tag === "required" ? missing : other).push(label);
  }
  const parts: string[] = [];
  if (missing.length > 0) parts.push(`Missing: ${missing.join(", ")}.`);
  if (other.length > 0) parts.push(`Invalid: ${other.join(", ")}.`);
  parts.push("Re-open Setup from the Lumi app, or add ?debug=true to enter them manually.");
  return parts.join(" ");
}

interface SetupProps {
  mode?: SetupMode;
}

// ── main page ─────────────────────────────────────────────────────────────────

export default function Setup({ mode = "initial" }: SetupProps = {}) {
  // #force in App.tsx forces mode="initial" for UI testing, but the lamp's
  // backend is still reachable in that scenario — so for feature-gating we
  // treat #force the same as continue (show Voice/Face sections, allow
  // prefill-driven checks, etc.). The redirect logic still keys off the raw
  // mode flag below since it should not auto-bounce during force testing.
  const forceHash = typeof window !== "undefined" && window.location.hash === "#force";
  const isContinue = mode === "continue" || forceHash;
  // Dev hosts (localhost / 127.0.0.1) are local Vite servers pointed at a
  // remote lamp — auto-bouncing to /monitor while debugging Setup is annoying.
  const isLocalDev = typeof window !== "undefined" &&
    (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");
  const [theme, toggleTheme, themeClass] = useTheme();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  useDocumentTitle("Setup");

  const channelParam = searchParams.get("channel");
  const initialChannel: ChannelType =
    channelParam === "slack" || channelParam === "discord" ? (channelParam as ChannelType) : "telegram";
  const [channel, setChannel] = useState<ChannelType>(initialChannel);

  const urlParams = useSetupUrlParams(searchParams);

  // When Lumi (golang) pushes provisioning credentials via query params, the
  // operator only needs to pick a Wi-Fi — every other field is already filled.
  // Treat presence of llm_api_key as the signal Lumi handed us a full config:
  // hide the AI Brain / Channels / Language / TTS menu entries and keep those
  // sections mounted (display:none) so their state still submits with the form.
  // Gated to initial (AP) mode so editing on the LAN IP keeps the full menu.
  const lumiPushedConfig = mode === "initial" && !!urlParams.llmApiKey;

  // Language + Lumi's Voice are gated behind ?debug=true: regular operators
  // get the auto-detected language and the "alloy"/openai voice defaults,
  // which still flow through submit because the sections stay in the DOM
  // (display:none) — same pattern as STT/MQTT below.
  const debug = searchParams.get("debug") === "true";

  // Default operator path: Lumi parent pushes config via URL params, so
  // AI Brain / Channels never need to be touched manually — sidebar entries
  // for them stay hidden unless ?debug=true. Manual fresh setup without
  // pushed params also requires ?debug=true to reach those sections.
  // STT (Deepgram) / MQTT are intentionally hidden — their state is still
  // wired up and submitted with empty or URL-prefilled defaults, so
  // re-adding a SectionCard + a SECTIONS entry brings them back without
  // other plumbing.
  const SECTIONS: { id: SectionId; label: string; icon: React.ReactNode }[] = [
    { id: "device", label: "Device", icon: <Lamp size={15} /> },
    { id: "wifi",   label: "Wi-Fi",  icon: <Wifi size={15} /> },
    ...(debug ? [
      { id: "llm" as SectionId,     label: "AI Brain",   icon: <Brain size={15} /> },
      { id: "channel" as SectionId, label: "Channels",   icon: <MessageSquare size={15} /> },
      { id: "language" as SectionId, label: "Language",  icon: <Globe size={15} /> },
      { id: "tts" as SectionId,     label: "Lumi's Voice", icon: <Volume2 size={15} /> },
    ] : []),
    // Voice / Face appear in continue mode only — they need the lamp's
    // hardware + backend, both unavailable while we're still on the AP.
    ...(isContinue ? [
      { id: "voice" as SectionId, label: "My Voice", icon: <Mic size={15} /> },
      { id: "face"  as SectionId, label: "Face",     icon: <UserCircle size={15} /> },
    ] : []),
  ];

  // When Lumi pushed config, the operator only needs Device + Wi-Fi visible —
  // the rest are filled from URL and submitted silently. Sections remain in
  // the DOM (see `lumiPushedConfig` display:none wrappers below) so values
  // still flow through the form; we just hide the menu entries.
  const visibleSections = lumiPushedConfig
    ? SECTIONS.filter((s) => s.id === "device" || s.id === "wifi")
    : SECTIONS;

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [setupWorking, setSetupWorking] = useState(false);
  // Setup phase mirrors the backend SetupStatus enum: connecting → connected
  // (success path) or failed. Drives the post-submit screen UI.
  const [setupPhase, setSetupPhase] = useState<"connecting" | "connected" | "failed">("connecting");
  const [setupLanIP, setSetupLanIP] = useState<string>("");
  const [setupErrorMsg, setSetupErrorMsg] = useState<string>("");
  // Always start on Device. The admin-password input lives there (fresh
  // devices need it; lumi-push doesn't carry that field via URL), so the
  // user must see it before submitting. For already-provisioned devices
  // useConfigPrefill detects cfg.device_id and skips device → wifi.
  const [activeSection, setActiveSection] = useState<SectionId>("device");
  const contentRef = useRef<HTMLDivElement>(null);

  const [deviceId, setDeviceId] = useState(urlParams.deviceId || "");
  const [mac, setMac] = useState("");
  // Admin password the operator picks here. Server bcrypts it into
  // config.admin_password_hash and uses it to gate browser admin access via
  // /api/login. Confirm field is a UI-only second copy — only adminPassword
  // ships to the server.
  const [adminPassword, setAdminPassword] = useState("");
  const [adminPasswordConfirm, setAdminPasswordConfirm] = useState("");
  // hasAdminPassword mirrors cfg.has_admin_password from /api/device/config.
  // True = device already has a bcrypt hash on file → hide the admin-password
  // fields and don't require them. False = first-time or migration device
  // missing the hash → show + require so the operator can't ship a setup
  // submit without one. Starts true to avoid flashing the fields during the
  // probe; useConfigPrefill flips it to false when the server reports missing.
  const [hasAdminPassword, setHasAdminPassword] = useState(true);
  // Mirrors cfg.has_network_password — when true, WifiSection swaps the
  // password input for a "configured" hint so the operator doesn't have to
  // retype a saved Wi-Fi password during re-setup via `#force`. Submit ships
  // an empty password; server merges from cfg.NetworkPassword pre-validation.
  const [hasNetworkPassword, setHasNetworkPassword] = useState(false);
  // mDNS hostname for the lamp on home Wi-Fi: `lumi-<suffix>.local`. Matches
  // what stage_ap sets via `hostnamectl set-hostname lumi-${SUFFIX_LC}` — both
  // sides derive the suffix from the device's hardware ID (Pi device-tree
  // serial / cpuinfo Serial / eth0 MAC, in that order) via the same logic in
  // lumi/internal/device/hardware.go.
  //
  // The backend returns `cfg.mac` already formatted as "Lumi-XXXX" (see
  // GetDeviceMac() — it prefixes "Lumi-" before the 4-char hex suffix), so
  // taking the last 4 chars and validating as hex gives us the canonical
  // suffix without depending on the prefix string. Empty when the config
  // hasn't returned yet, or when the device couldn't determine a serial.
  const lumiMdnsHost = useMemo(() => {
    const tail = (mac || "").trim().toLowerCase().slice(-4);
    if (!/^[0-9a-f]{4}$/.test(tail)) return "";
    return `lumi-${tail}`;
  }, [mac]);
  const [llmApiKey, setLlmApiKey] = useState(urlParams.llmApiKey || "");
  const [llmUrl, setLlmUrl] = useState(urlParams.llmUrl || "");
  const [llmModel, setLlmModel] = useState(urlParams.llmModel || "");
  // Snapshot of AI Brain fields populated when entering setup (URL or saved
  // config). Populated values render with the Edit pencil so re-running setup
  // doesn't accidentally overwrite credentials.
  const [llmLoaded, setLlmLoaded] = useState<LlmLoadedState>({
    apiKey: !!urlParams.llmApiKey,
    baseUrl: !!urlParams.llmUrl,
    model: !!urlParams.llmModel,
  });
  const [llmDisableThinking, setLlmDisableThinking] = useState(false);
  // deepgram input is hidden in this build; submit reads urlParams.deepgramApiKey directly
  const [ttsApiKey, setTtsApiKey] = useState(urlParams.ttsApiKey || "");
  const [ttsBaseUrl, setTtsBaseUrl] = useState(urlParams.ttsBaseUrl || "");
  // STT credentials are not exposed in Setup UI but still saved to config so
  // the device's voice pipeline has fallback values mirroring the LLM endpoint.
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
  // Pre-fill STT language from URL param, else browser locale so VN/CN buyers
  // don't have to touch this field; users can still override before submitting.
  // URL value is validated against the dropdown allow-list — server stores
  // anything we send (no validation upstream), so we gate at the FE boundary.
  // Final fallback is "en" (rather than empty) so the saved config always has
  // a sensible default the agent can lean on.
  const [sttLanguage, setSttLanguage] = useState<string>(() => {
    const VALID = ["en", "vi", "zh-CN", "zh-TW"];
    if (urlParams.sttLanguage) {
      if (VALID.includes(urlParams.sttLanguage)) return urlParams.sttLanguage;
      console.warn(`[setup] URL stt_language="${urlParams.sttLanguage}" not in ${VALID.join(",")}, ignoring`);
    }
    const loc = (navigator.language || "").toLowerCase();
    if (loc.startsWith("vi")) return "vi";
    if (loc.startsWith("zh-tw") || loc.startsWith("zh-hant") || loc.startsWith("zh-hk")) return "zh-TW";
    if (loc.startsWith("zh")) return "zh-CN";
    if (loc.startsWith("en")) return "en";
    return "en";
  });
  const [ttsProvider, setTtsProvider] = useState(urlParams.ttsProvider || "openai");
  const [ttsVoice, setTtsVoice] = useState(urlParams.ttsVoice || "alloy");
  const { ttsProviders, ttsVoices } = useTTSCatalog({
    ttsProvider, sttLanguage, ttsVoice,
    urlProvider: urlParams.ttsProvider,
    urlVoice: urlParams.ttsVoice,
    setTtsProvider, setTtsVoice,
  });
  const [teleToken, setTeleToken] = useState(urlParams.teleToken || "");
  const [teleUserId, setTeleUserId] = useState(urlParams.teleUserId || "");
  const [slackBotToken, setSlackBotToken] = useState(urlParams.slackBotToken || "");
  const [slackAppToken, setSlackAppToken] = useState(urlParams.slackAppToken || "");
  const [slackUserId, setSlackUserId] = useState(urlParams.slackUserId || "");
  const [discordBotToken, setDiscordBotToken] = useState(urlParams.discordBotToken || "");
  const [discordGuildId, setDiscordGuildId] = useState(urlParams.discordGuildId || "");
  const [discordUserId, setDiscordUserId] = useState(urlParams.discordUserId || "");
  // Snapshot of channel credentials populated when entering Setup. Filled
  // values render with the Edit pencil to prevent accidental overwrites.
  const [channelLoaded, setChannelLoaded] = useState<ChannelLoadedState>({
    teleToken: !!urlParams.teleToken, teleUserId: !!urlParams.teleUserId,
    slackBotToken: !!urlParams.slackBotToken, slackAppToken: !!urlParams.slackAppToken,
    slackUserId: !!urlParams.slackUserId,
    discordBotToken: !!urlParams.discordBotToken, discordGuildId: !!urlParams.discordGuildId,
    discordUserId: !!urlParams.discordUserId,
  });
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");

  // Face enroll — same flow as EditConfig.Face. Uses /api/hardware/face endpoints
  // directly; only relevant in continue mode (lamp online).
  const {
    faceName, setFaceName,
    faceFiles, setFaceFiles,
    faceUploading,
    faceMsg,
    faceInputRef,
    faceOwners,
    loadFaceOwners,
    removeFaceOwner,
    handleFaceEnroll,
  } = useFaceEnroll();

  // Voice enroll state + handlers live inside VoiceSection (continue mode
  // only) — nothing outside reads them. After each enroll the section calls
  // loadFaceOwners so new samples surface in the enrolled list.

  // Per-section "done" detection drives the ✓ checkmark in the sidebar and
  // the auto-scroll-to-next-pending behavior in continue mode. We treat a
  // section as done when its config has the value the user came here to set.
  // Secret fields don't round-trip through GET /api/device/config anymore
  // (ConfigPublicResponse returns has_* booleans only), so check `*Loaded`
  // alongside the raw form value — operator typing into the field also
  // counts as done, but a saved-but-not-retyped secret still shows the green
  // tick from its presence boolean.
  const sectionDone: Record<SectionId, boolean> = {
    // device-section is "done" when a device id exists AND, if the lamp has
    // no admin password on file yet, the operator has filled + confirmed one.
    // Devices that already have a hash satisfy the gate automatically.
    device: !!deviceId && (hasAdminPassword || (!!adminPassword && adminPassword === adminPasswordConfirm)),
    wifi: !!ssid,
    llm: !!llmApiKey || llmLoaded.apiKey,
    language: true, // Auto/empty is a valid choice — never block on this.
    channel: channel === "telegram"
      ? (!!teleToken || channelLoaded.teleToken)
      : channel === "slack"
        ? (!!slackBotToken || channelLoaded.slackBotToken)
        : (!!discordBotToken || channelLoaded.discordBotToken),
    tts: !!ttsVoice,
    voice: faceOwners.some((p) => (p.voice_samples?.length ?? 0) > 0),
    face: faceOwners.some((p) => p.photo_count > 0),
    deepgram: true,
    mqtt: true,
    stt: true, // EditConfig's alias for language; not rendered in Setup.
  };

  useEffect(() => {
    setMqttEndpoint((prev) => prev || urlParams.mqttEndpoint);
    setMqttPort((prev) => prev || urlParams.mqttPort);
    setMqttUsername((prev) => prev || urlParams.mqttUsername);
    setMqttPassword((prev) => prev || urlParams.mqttPassword);
    setFaChannel((prev) => prev || urlParams.faChannel);
    setFdChannel((prev) => prev || urlParams.fdChannel);
  }, [urlParams]);

  // Continue mode: refresh enrolled face/voice owners (lamp is online now).
  useEffect(() => {
    if (isContinue) loadFaceOwners();
  }, [isContinue, loadFaceOwners]);

  // Continue mode: scroll the user to the first section that still needs
  // attention so they can see what's left to do without hunting through
  // the sidebar. If every required section is already done on first load,
  // bounce straight to /monitor — Setup has nothing left to ask for.
  // autoScrolledRef doubles as the "we previously saw at least one incomplete
  // section" flag. Redirect only fires after we've scrolled the user to a
  // pending section at least once — i.e. they were actively running setup,
  // and the last pending field just became done. When the user opens /setup
  // post-completion to view/edit, the flag stays false, so we stay on the
  // page with checks visible instead of bouncing them to /monitor.
  const autoScrolledRef = useRef(false);
  useEffect(() => {
    if (!isContinue) return;
    if (!llmApiKey) return; // wait until config has loaded
    const required: SectionId[] = ["device", "wifi", "llm", "channel", "tts", "voice", "face"];
    // Redirect any time all required sections become done — including later
    // ticks when async data (e.g. faceOwners) arrives after first paint. This
    // path is NOT gated by autoScrolledRef on purpose; otherwise the first
    // effect run (before faceOwners loaded) sets the ref and the redirect
    // never fires once enrollment counts come back.
    if (required.every((id) => sectionDone[id])) {
      // Skip auto-bounce when user is on #force testing the UI on a
      // provisioned device, or when running on a local dev host pointed at a
      // remote lamp — they want to see the page, not jump away.
      if (autoScrolledRef.current && !forceHash && !isLocalDev) navigate("/monitor", { replace: true });
      return;
    }
    if (autoScrolledRef.current) return;
    const order: SectionId[] = ["device", "wifi", "llm", "channel", "language", "tts", "voice", "face"];
    const next = order.find((id) => !sectionDone[id]) ?? "tts";
    setActiveSection(next);
    autoScrolledRef.current = true;
  }, [isContinue, llmApiKey, sectionDone, navigate]);

  // Wi-Fi scan with retry — kept inline since it's specific to this page.
  useEffect(() => {
    const maxAttempts = 4;
    let attempt = 0;
    function fetchNetworks(): Promise<void> {
      attempt += 1;
      return getNetworks()
        .then((nets) => setNetworks((nets ?? []).filter((n) => n.ssid !== "")))
        .catch(() => { if (attempt < maxAttempts) return fetchNetworks(); setNetworks([]); });
    }
    fetchNetworks().finally(() => setLoadingList(false));
  }, []);

  useConfigPrefill({
    urlParams, channelParam,
    setTtsProvider, setTtsVoice, setSsid, setDeviceId, setMac, setActiveSection,
    setLlmUrl, setLlmModel, setLlmLoaded, setLlmDisableThinking,
    setTtsBaseUrl,
    setChannelLoaded,
    setTeleUserId,
    setSlackUserId,
    setDiscordGuildId, setDiscordUserId,
    setChannel,
    setMqttEndpoint, setMqttPort, setMqttUsername,
    setFaChannel, setFdChannel,
    setSttLanguage,
    setHasAdminPassword,
    setHasNetworkPassword,
  });

  useSetupStatusPolling({
    setupWorking, setupPhase, setupLanIP, lumiMdnsHost,
    setSetupPhase, setSetupLanIP, setSetupErrorMsg,
  });


  // Auto-mirror AI Brain key/URL into TTS while TTS field is empty.
  // Once the user types into TTS the sync stops; clearing it re-enables mirroring.
  useEffect(() => {
    if (!ttsApiKey && llmApiKey) setTtsApiKey(llmApiKey);
  }, [llmApiKey, ttsApiKey]);
  useEffect(() => {
    if (!ttsBaseUrl && llmUrl) setTtsBaseUrl(llmUrl);
  }, [llmUrl, ttsBaseUrl]);
  // Same for STT (no UI in Setup — silently mirrors LLM into config).
  useEffect(() => {
    if (!sttApiKey && llmApiKey) setSttApiKey(llmApiKey);
  }, [llmApiKey, sttApiKey]);
  useEffect(() => {
    if (!sttBaseUrl && llmUrl) setSttBaseUrl(llmUrl);
  }, [llmUrl, sttBaseUrl]);

  const scrollTo = (id: SectionId) => {
    setActiveSection(id);
    // Pop the content area back to the top so a Back/Next click never lands
    // the operator mid-scroll of the previous section.
    contentRef.current?.scrollTo({ top: 0 });
  };

  // Wizard-style step navigation: Prev/Next walk through visibleSections; the
  // submit button only renders on the last visible step. Auto-scroll edge
  // cases (activeSection on a hidden section) fall back to index 0 so Next
  // still advances into the visible set.
  const currentStepIndex = Math.max(0, visibleSections.findIndex((s) => s.id === activeSection));
  const isFirstStep = currentStepIndex === 0;
  const isLastStep = currentStepIndex >= visibleSections.length - 1;
  const goPrev = () => {
    if (isFirstStep) return;
    scrollTo(visibleSections[currentStepIndex - 1].id);
  };
  const goNext = () => {
    if (isLastStep) return;
    scrollTo(visibleSections[currentStepIndex + 1].id);
  };

  const uniqueNetworks = useMemo(
    () => [...new Map(networks.filter((n) => n.ssid !== "").map((n) => [n.ssid, n])).values()],
    [networks],
  );

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Require an admin password only when the device doesn't already have
    // one on file. Already-provisioned devices that pre-date the Login UI
    // batch land here with hasAdminPassword=false and must pick one now;
    // devices that have a hash skip the check entirely.
    if (!hasAdminPassword) {
      if (!adminPassword) {
        setError("Pick an admin password — you'll use it to sign in later.");
        return;
      }
      if (adminPassword !== adminPasswordConfirm) {
        setError("Admin password and confirmation don't match.");
        return;
      }
    }
    // Pre-flight check for the two visible Wi-Fi fields. Catches implicit
    // Enter-key form submission and any other accidental fire-before-ready
    // path with a plain hint instead of letting the Go validator return a
    // tag-format error. Other required fields (LLM creds, device ID, channel
    // tokens) ride through URL params or the saved config merge on the
    // backend, so we let the server be the source of truth for those — see
    // the normaliseSetupError() catch below for friendlier rendering.
    if (!ssid.trim()) {
      setError("Choose a Wi-Fi network before continuing.");
      setActiveSection("wifi");
      return;
    }
    if (!password && !hasNetworkPassword) {
      setError("Enter the Wi-Fi password.");
      setActiveSection("wifi");
      return;
    }
    setLoading(true);
    try {
      let channelCredentials: Record<string, string>;
      switch (channel) {
        case "telegram":
          channelCredentials = {
            telegram_bot_token: urlParams.teleToken || teleToken,
            telegram_user_id: urlParams.teleUserId || teleUserId,
          };
          break;
        case "slack":
          channelCredentials = {
            slack_bot_token: urlParams.slackBotToken || slackBotToken,
            slack_app_token: urlParams.slackAppToken || slackAppToken,
            slack_user_id: urlParams.slackUserId || slackUserId,
          };
          break;
        default:
          channelCredentials = {
            discord_bot_token: urlParams.discordBotToken || discordBotToken,
            discord_guild_id: urlParams.discordGuildId || discordGuildId,
            discord_user_id: urlParams.discordUserId || discordUserId,
          };
      }
      const body: Parameters<typeof setupDevice>[0] = {
        ssid: ssid.trim(), password, channel,
        ...channelCredentials,
        llm_base_url: urlParams.llmUrl || llmUrl,
        llm_api_key: urlParams.llmApiKey || llmApiKey,
        llm_model: urlParams.llmModel || llmModel,
        llm_disable_thinking: llmDisableThinking || undefined,
        deepgram_api_key: urlParams.deepgramApiKey || undefined,
        stt_api_key: sttApiKey || undefined,
        stt_base_url: sttBaseUrl || undefined,
        stt_language: sttLanguage || undefined,
        tts_api_key: ttsApiKey || undefined,
        tts_base_url: ttsBaseUrl || undefined,
        tts_provider: ttsProvider || undefined,
        tts_voice: ttsVoice || undefined,
        device_id: urlParams.deviceId || deviceId,
        admin_password: adminPassword || undefined,
      };
      const endpoint = mqttEndpoint || urlParams.mqttEndpoint;
      if (endpoint) {
        const port = mqttPort || urlParams.mqttPort;
        Object.assign(body, {
          mqtt_endpoint: endpoint,
          mqtt_port: port ? parseInt(port, 10) : 1883,
          mqtt_username: mqttUsername || urlParams.mqttUsername || undefined,
          mqtt_password: mqttPassword || urlParams.mqttPassword || undefined,
          fa_channel: faChannel || urlParams.faChannel || undefined,
          fd_channel: fdChannel || urlParams.fdChannel || undefined,
        });
      }
      const result = await setupDevice(body);
      setSetupWorking(result);
      setSetupPhase("connecting");
    } catch (err) {
      setError(normaliseSetupError(err instanceof Error ? err.message : "Setup failed."));
    }
    setLoading(false);
  }, [
    channel, urlParams, teleToken, teleUserId, slackBotToken, slackAppToken, slackUserId,
    discordBotToken, discordGuildId, discordUserId, ssid, password, llmUrl, llmApiKey,
    llmModel, llmDisableThinking, sttApiKey, sttBaseUrl, ttsApiKey, ttsBaseUrl, ttsVoice, deviceId,
    mqttEndpoint, mqttPort, mqttUsername, mqttPassword, faChannel, fdChannel,
    sttLanguage, ttsProvider, isContinue, adminPassword, adminPasswordConfirm,
    hasAdminPassword, hasNetworkPassword,
  ]);

  return (
    <div className={`lm-root lm-setup ${themeClass}`} style={{
      display: "flex", height: "100vh",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
    }}>
      <style>{`
        @media (max-width: 640px) {
          .lm-setup .lm-sidebar { display: none !important; }
          .lm-setup .lm-mobile-tabs { display: flex !important; }
          .lm-setup .lm-main-content { padding: 16px !important; }
        }
      `}</style>

      {/* ── Sidebar (hidden on mobile) ── */}
      <aside className="lm-sidebar" style={{
        width: 192, flexShrink: 0,
        background: C.sidebar, borderRight: `1px solid ${C.border}`,
        display: "flex", flexDirection: "column",
      }}>

        <nav style={{ padding: "10px 0", flex: 1 }}>
          {visibleSections.map((s) => {
            const active = activeSection === s.id;
            // Show checks whenever a section's value is filled — including in
            // #force (initial) mode if the lamp already has saved config to
            // prefill from. A truly empty device still shows zero checks
            // because sectionDone returns false across the board.
            const done = sectionDone[s.id];
            return (
              <button key={s.id} onClick={() => scrollTo(s.id)} style={{
                display: "flex", alignItems: "center", gap: 9,
                padding: "8px 14px", borderRadius: 8, margin: "2px 8px",
                fontSize: 12.5, fontWeight: active ? 600 : 400,
                color: active ? C.amber : (done ? C.textMuted : "var(--lm-text-dim)"),
                background: active ? C.amberDim : "transparent",
                cursor: "pointer", transition: "all 0.15s",
                border: "none", width: "calc(100% - 16px)", textAlign: "left",
              }}>
                {s.icon}
                <span style={{ flex: 1 }}>{s.label}</span>
                {done && <Check size={13} style={{ color: C.green }} />}
              </button>
            );
          })}
        </nav>

        <div style={{ padding: "12px 16px", borderTop: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <a href="/monitor" style={{
            display: "flex", alignItems: "center", gap: 7,
            color: C.textMuted, textDecoration: "none", fontSize: 12,
            transition: "color 0.15s",
          }}
            onMouseEnter={(e) => (e.currentTarget.style.color = C.textDim)}
            onMouseLeave={(e) => (e.currentTarget.style.color = C.textMuted)}
          >
            ← Monitor
          </a>
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 4px",
          }} title={`Theme: ${theme}`}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Mobile tabs (hidden on desktop) */}
        <div className="lm-mobile-tabs" style={{
          display: "none", overflowX: "auto", gap: 4, padding: "8px 12px",
          borderBottom: `1px solid ${C.border}`, flexShrink: 0, alignItems: "center",
        }}>
          {visibleSections.map((s) => {
            const active = activeSection === s.id;
            return (
              <button key={s.id} onClick={() => scrollTo(s.id)} style={{
                padding: "5px 10px", borderRadius: 6, fontSize: 11, fontWeight: active ? 600 : 400,
                color: active ? C.amber : C.textDim,
                background: active ? C.amberDim : "transparent",
                border: "none", cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0,
              }}>
                {s.label}
              </button>
            );
          })}
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 6px", marginLeft: "auto", flexShrink: 0,
          }}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>

        {/* Topbar */}
        <div style={{
          padding: "10px 24px", borderBottom: `1px solid ${C.border}`,
          display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>
            {setupWorking ? "Setting up…" : SECTIONS.find((s) => s.id === activeSection)?.label ?? "Wi-Fi"}
          </span>
          {/* Submit lives at the bottom of the form alongside Back/Next so the
              operator follows a single wizard flow per step. */}
          {!setupWorking && !isFirstStep && (
            <span style={{ fontSize: 11, color: C.textDim }}>
              Step {currentStepIndex + 1} / {visibleSections.length}
            </span>
          )}
        </div>

        {/* Content */}
        <div ref={contentRef} className="lm-fade-in lm-main-content" style={{
          flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px",
        }}>
          <div style={{ maxWidth: 560, margin: "0 auto" }}>

            {/* Post-submit screen: shows progress while the lamp joins
                Wi-Fi, then a QR + IP for the user to continue setup on the
                home network once the AP shuts down. */}
            {setupWorking ? (
              <div style={{
                background: C.card, border: `1px solid ${C.border}`,
                borderRadius: 12, padding: "32px 24px", textAlign: "center",
              }}>
                {setupPhase === "connecting" && (
                  <>
                    <div style={{ fontSize: 32, marginBottom: 12 }}>⏳</div>
                    <div style={{ fontSize: 15, fontWeight: 600, color: C.amber, marginBottom: 8 }}>
                      Lumi is joining your Wi-Fi…
                    </div>
                    <div style={{ fontSize: 12, color: C.textDim, marginBottom: lumiMdnsHost ? 18 : 0 }}>
                      This usually takes 10-30 seconds. Stay on this network.
                    </div>
                    {/* Fallback manual link: the auto-redirect can fail when
                        the user's network blocks mDNS (Android Chrome) or
                        when the AP shuts down before the phase poll flips
                        to "connected". Offering the same .local link here
                        means a stuck operator can always click their way
                        out by reconnecting to home Wi-Fi first. */}
                    {lumiMdnsHost && (
                      <div style={{
                        marginTop: 6, paddingTop: 14,
                        borderTop: `1px solid ${C.border}`,
                        fontSize: 11, color: C.textMuted, lineHeight: 1.55,
                      }}>
                        Stuck here? Reconnect to your home Wi-Fi, then open{" "}
                        <a
                          href={`http://${lumiMdnsHost}.local${window.location.pathname}${getInitialSearch()}`}
                          style={{
                            color: C.amber, textDecoration: "none",
                            fontFamily: "ui-monospace, monospace",
                          }}
                        >
                          http://{lumiMdnsHost}.local/
                        </a>
                        .
                      </div>
                    )}
                  </>
                )}

                {setupPhase === "connected" && (
                  <>
                    <div style={{ fontSize: 32, marginBottom: 12 }}>✦</div>
                    <div style={{ fontSize: 15, fontWeight: 600, color: C.amber, marginBottom: 16 }}>
                      Lumi is online!
                    </div>

                    {/* mDNS path (primary): the lamp publishes
                        `lumi-<last4mac>.local` via avahi-daemon, so we don't
                        need to know its new LAN IP to redirect — the browser
                        resolves it once the user is on home Wi-Fi.
                        Supported out-of-box: Windows 10 1803+, Windows 11,
                        macOS, iOS, most desktop Linux. Falls back to a
                        router-admin hint when the host is unreachable. */}
                    {lumiMdnsHost ? (
                      <>
                        <div style={{ fontSize: 13, color: C.text, marginBottom: 4, fontFamily: "ui-monospace, monospace" }}>
                          http://{lumiMdnsHost}.local/
                        </div>
                        <div style={{ fontSize: 11, color: C.textDim, marginBottom: 18, lineHeight: 1.5 }}>
                          Reconnect your computer to your home Wi-Fi, then click
                          Continue.
                        </div>
                        <a
                          // Carry the current pathname + query params so any
                          // ?llm_api_key=… etc. from Lumi remain in scope on
                          // the new host (redundant — lamp already persisted
                          // them via submit — but cheap and useful when the
                          // operator re-runs setup with different overrides).
                          // Force reload when the user is already on the
                          // canonical .local URL — otherwise the browser
                          // no-ops the same-URL click and they stay stuck on
                          // the "Lumi is online!" screen even though the lamp
                          // is reachable in continue mode now.
                          href={`http://${lumiMdnsHost}.local${window.location.pathname}${getInitialSearch()}`}
                          onClick={(e) => {
                            if (window.location.hostname === `${lumiMdnsHost}.local`) {
                              e.preventDefault();
                              window.location.reload();
                            }
                          }}
                          style={{
                            display: "inline-block", padding: "9px 18px",
                            background: C.amber, color: "#fff",
                            borderRadius: 7, fontSize: 12.5, fontWeight: 600,
                            textDecoration: "none",
                            marginBottom: 14,
                          }}
                        >
                          Continue setup →
                        </a>
                        <div style={{ fontSize: 10.5, color: C.textMuted, lineHeight: 1.5 }}>
                          Can't reach it?
                          {setupLanIP && (
                            <> Try <span style={{ fontFamily: "ui-monospace, monospace" }}>http://{setupLanIP}/</span> or </>
                          )}
                          {" "}find Lumi's IP in your router's admin page (look
                          for "{lumiMdnsHost}").
                        </div>
                      </>
                    ) : (
                      <div style={{ fontSize: 12, color: C.textDim }}>
                        Lamp connected. Open your router's admin page to find
                        the device's IP address (look for "lumi-").
                      </div>
                    )}
                  </>
                )}

                {setupPhase === "failed" && (
                  <>
                    <div style={{ fontSize: 32, marginBottom: 12, color: C.red }}>✕</div>
                    <div style={{ fontSize: 15, fontWeight: 600, color: C.red, marginBottom: 8 }}>
                      Wi-Fi setup failed
                    </div>
                    <div style={{ fontSize: 12, color: C.textDim, marginBottom: 14 }}>
                      {setupErrorMsg || "Couldn't connect to the network you chose. Double-check the password and try again."}
                    </div>
                    <button
                      type="button"
                      onClick={() => { setSetupWorking(false); setSetupPhase("connecting"); }}
                      style={{
                        padding: "8px 16px", background: C.amber, color: "#fff",
                        border: "none", borderRadius: 7, fontSize: 12, cursor: "pointer", fontWeight: 600,
                      }}
                    >
                      Back to setup
                    </button>
                  </>
                )}
              </div>
            ) : (
              <>
                {error && (
                  <div style={{
                    background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
                    borderRadius: 8, padding: "10px 14px", fontSize: 12, color: C.red, marginBottom: 16,
                  }}>
                    {error}
                  </div>
                )}

                <form id="setup-form" onSubmit={handleSubmit} noValidate>

                  <DeviceSection
                    active={activeSection === "device"}
                    deviceId={deviceId} setDeviceId={setDeviceId}
                    mac={mac}
                    {...(hasAdminPassword ? {} : {
                      adminPassword,
                      setAdminPassword,
                      adminPasswordConfirm,
                      setAdminPasswordConfirm,
                    })}
                  />

                  <WifiSection
                    active={activeSection === "wifi"}
                    ssid={ssid} setSsid={setSsid}
                    password={password} setPassword={setPassword}
                    passwordConfigured={hasNetworkPassword && !password}
                    loadingList={loadingList}
                    uniqueNetworks={uniqueNetworks}
                  />

                  {/* When lumiPushedConfig is on, the four sections below are
                      kept mounted but visually hidden — their state autofills
                      from URL params and still flows through the form submit. */}
                  <div style={lumiPushedConfig ? { display: "none" } : undefined}>
                    <LLMSection
                      active={lumiPushedConfig || activeSection === "llm"}
                      llmLoaded={llmLoaded}
                      llmApiKey={llmApiKey} setLlmApiKey={setLlmApiKey}
                      llmUrl={llmUrl} setLlmUrl={setLlmUrl}
                      llmModel={llmModel} setLlmModel={setLlmModel}
                    />

                    <ChannelSection
                      active={lumiPushedConfig || activeSection === "channel"}
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

                    <LanguageSection
                      active={lumiPushedConfig || activeSection === "language"}
                      sttLanguage={sttLanguage} setSttLanguage={setSttLanguage}
                    />

                    <TTSSection
                      active={lumiPushedConfig || activeSection === "tts"}
                      isContinue={isContinue}
                      ttsProvider={ttsProvider} setTtsProvider={setTtsProvider}
                      ttsProviders={ttsProviders}
                      ttsVoice={ttsVoice} setTtsVoice={setTtsVoice}
                      ttsVoices={ttsVoices}
                      sttLanguage={sttLanguage}
                    />
                  </div>

                  {isContinue && (
                    <VoiceSection
                      active={activeSection === "voice"}
                      sttLanguage={sttLanguage}
                      faceOwners={faceOwners}
                      loadFaceOwners={loadFaceOwners}
                    />
                  )}

                  {isContinue && (
                    <FaceSection
                      active={activeSection === "face"}
                      faceName={faceName} setFaceName={setFaceName}
                      faceFiles={faceFiles} setFaceFiles={setFaceFiles}
                      faceUploading={faceUploading}
                      faceMsg={faceMsg}
                      faceInputRef={faceInputRef}
                      faceOwners={faceOwners}
                      removeFaceOwner={removeFaceOwner}
                      handleFaceEnroll={handleFaceEnroll}
                    />
                  )}

                  <div style={{
                    display: "flex", gap: 10, justifyContent: "space-between",
                    alignItems: "center", marginTop: 8,
                  }}>
                    {isFirstStep ? <span /> : (
                      <button
                        type="button"
                        onClick={goPrev}
                        style={{
                          padding: "9px 18px", borderRadius: 7, fontSize: 12.5, fontWeight: 500,
                          background: C.surface, color: C.text,
                          border: `1px solid ${C.border}`, cursor: "pointer",
                        }}
                      >
                        ← Back
                      </button>
                    )}
                    {isLastStep ? (
                      isContinue ? (
                        // Continue mode = device already provisioned + on
                        // home Wi-Fi. Voice / Face are optional enrollments,
                        // so the last step shouldn't re-trigger setup — send
                        // the user to /monitor instead. Re-submit only
                        // happens in initial mode (last step = wifi or tts).
                        <button
                          key="done"
                          type="button"
                          onClick={() => navigate("/monitor")}
                          style={{
                            padding: "9px 22px", borderRadius: 7, fontSize: 12.5, fontWeight: 600,
                            background: C.amber, color: "#0C0B09",
                            border: "none", cursor: "pointer",
                          }}
                        >
                          Go to monitor →
                        </button>
                      ) : (
                        <button
                          // Distinct keys prevent React from mutating a single
                          // <button> element from type="button" (Next) to
                          // type="submit" (Setup) in place. Without separate
                          // keys the in-flight click on Next can land on the
                          // mutated Submit button and trigger an unwanted form
                          // submission.
                          key="submit"
                          type="submit"
                          disabled={loading || loadingList}
                          style={{
                            padding: "9px 22px", borderRadius: 7, fontSize: 12.5, fontWeight: 600,
                            background: loading || loadingList ? C.surface : C.amber,
                            color: loading || loadingList ? C.textMuted : "#0C0B09",
                            border: "none",
                            cursor: loading || loadingList ? "not-allowed" : "pointer",
                            opacity: loading || loadingList ? 0.6 : 1,
                          }}
                        >
                          {loading ? "Setting up…" : "Setup"}
                        </button>
                      )
                    ) : (
                      <button
                        key="next"
                        type="button"
                        onClick={goNext}
                        style={{
                          padding: "9px 22px", borderRadius: 7, fontSize: 12.5, fontWeight: 600,
                          background: C.amber, color: "#0C0B09",
                          border: "none", cursor: "pointer",
                        }}
                      >
                        Next →
                      </button>
                    )}
                  </div>

                </form>
              </>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
