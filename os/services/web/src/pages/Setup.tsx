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
import { setupBridge } from "@/lib/setupBridge";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "@/hooks/setup/types";
import { C, ADMIN_PASSWORD_MIN } from "@/components/setup/shared";
import { DeviceSection } from "@/components/setup/DeviceSection";
import { WifiSection } from "@/components/setup/WifiSection";
import { LLMSection } from "@/components/setup/LLMSection";
import { ChannelSection } from "@/components/setup/ChannelSection";
import { LanguageSection } from "@/components/setup/LanguageSection";
import { TTSSection } from "@/components/setup/TTSSection";
import { VoiceSection } from "@/components/setup/VoiceSection";
import { FaceSection } from "@/components/setup/FaceSection";
import type { ChannelType, NetworkItem } from "@/types";
import { Wifi, Cpu, Brain, Volume2, MessageSquare, UserCircle, Mic, Globe, Check, XCircle, CheckCircle2 } from "lucide-react";

// ── Setup wizard version switch ─────────────────────────────────────────────
// Flip this single constant to swap the whole Setup flow, then rebuild:
//   1 → V1: two steps. A dedicated "Device" step (admin password + confirm,
//           hidden by default with an eye toggle) precedes the "Wi-Fi" step.
//   2 → V2: one step. The admin password ("Device password", shown by default)
//           is merged into the top of the "Wi-Fi" step; no separate Device step,
//           no confirm field.
// Everything that differs between the two versions branches on `isV1`, so
// reverting business decisions is a one-line change. Default: V2 (current).
type SetupVersion = 1 | 2;
const SETUP_VERSION = 2 as SetupVersion;
const isV1 = SETUP_VERSION === 1;

// SetupMode controls which sections render. Initial = AP/offline (hide
// online-only enrollments + tests), Continue = LAN/online (the device can hit
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
  parts.push("Re-open Setup from the companion app, or add ?debug=true to enter them manually.");
  return parts.join(" ");
}

interface SetupProps {
  mode?: SetupMode;
}

// CopyAddress — a device URL with a one-tap Copy button. Shown on the
// post-submit screen so the operator can capture the address BEFORE they
// switch Wi-Fi networks (at which point this page loses its connection and any
// un-copied address is gone). Pass the full URL via `url` — callers prefer the
// raw-IP address (works on every LAN) over the .local name (fails when the
// router blocks mDNS).
//
// Clipboard: the Setup page is served over plain HTTP (http://192.168.100.1),
// where `navigator.clipboard` is undefined (it requires a secure context), so
// the modern API silently no-ops. Fall back to a hidden-textarea +
// document.execCommand("copy"), which works on http:// origins, so the button
// actually copies instead of doing nothing.
function CopyAddress({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  const text = url;
  const flashCopied = () => {
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };
  const legacyCopy = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      // Keep it off-screen and non-disruptive to scroll/focus.
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      ta.setAttribute("readonly", "");
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) flashCopied();
    } catch {
      /* copy unsupported — user can still select the text manually */
    }
  };
  const copy = () => {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(flashCopied, legacyCopy);
    } else {
      legacyCopy();
    }
  };
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 8, padding: "8px 10px",
    }}>
      <span style={{
        flex: 1, textAlign: "left", fontSize: 13.5, color: C.text,
        fontFamily: "ui-monospace, monospace", overflow: "hidden", textOverflow: "ellipsis",
      }}>
        {text}
      </span>
      <button
        type="button"
        onClick={copy}
        className={`lm-btn lm-btn-ghost${copied ? " lm-copied" : ""}`}
        style={{ padding: "5px 10px", fontSize: 11, flexShrink: 0, transition: "background 0.15s, color 0.15s, border-color 0.15s", minWidth: 64 }}
      >
        {copied ? "Copied ✓" : "Copy"}
      </button>
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────────

export default function Setup({ mode = "initial" }: SetupProps = {}) {
  // #force in App.tsx forces mode="initial" for UI testing, but the device's
  // backend is still reachable in that scenario — so for feature-gating we
  // treat #force the same as continue (show Voice/Face sections, allow
  // prefill-driven checks, etc.). The redirect logic still keys off the raw
  // mode flag below since it should not auto-bounce during force testing.
  const forceHash = typeof window !== "undefined" && window.location.hash === "#force";
  const isContinue = mode === "continue" || forceHash;
  // Dev hosts (localhost / 127.0.0.1) are local Vite servers pointed at a
  // remote device — auto-bouncing to /monitor while debugging Setup is annoying.
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

  // When the OS server (golang) pushes provisioning credentials via query params, the
  // operator only needs to pick a Wi-Fi — every other field is already filled.
  // Treat presence of llm_api_key as the signal the OS server handed us a full config:
  // hide the AI Brain / Channels / Language / TTS menu entries and keep those
  // sections mounted (display:none) so their state still submits with the form.
  // Gated to initial (AP) mode so editing on the LAN IP keeps the full menu.
  const devicePushedConfig = mode === "initial" && !!urlParams.llmApiKey;

  // Language + Lamp's Voice are gated behind ?debug=true: regular operators
  // get the auto-detected language and the "alloy"/openai voice defaults,
  // which still flow through submit because the sections stay in the DOM
  // (display:none) — same pattern as STT/MQTT below.
  const debug = searchParams.get("debug") === "true";

  // Default operator path: the OS server parent pushes config via URL params, so
  // AI Brain / Channels never need to be touched manually — sidebar entries
  // for them stay hidden unless ?debug=true. Manual fresh setup without
  // pushed params also requires ?debug=true to reach those sections.
  // STT (Deepgram) / MQTT are intentionally hidden — their state is still
  // wired up and submitted with empty or URL-prefilled defaults, so
  // re-adding a SectionCard + a SECTIONS entry brings them back without
  // other plumbing.
  // `optional` flags the enrollment steps (Voice/Face) that the operator can
  // skip — drives the "Optional" sidebar tag and the Skip button so they don't
  // feel like a blocking requirement. Required steps omit the flag.
  const SECTIONS: { id: SectionId; label: string; icon: React.ReactNode; optional?: boolean }[] = [
    // V1: dedicated Device step (admin password + confirm). V2: the Device step
    // is dropped — the admin password moves into the Wi-Fi step, and Device ID /
    // MAC become read-only metadata kept mounted but hidden (see the form below)
    // so they still flow through submit.
    ...(isV1 ? [{ id: "device" as SectionId, label: "Device", icon: <Cpu size={15} /> }] : []),
    { id: "wifi",   label: "Wi-Fi",  icon: <Wifi size={15} /> },
    ...(debug ? [
      { id: "llm" as SectionId,     label: "AI Brain",   icon: <Brain size={15} /> },
      { id: "channel" as SectionId, label: "Channels",   icon: <MessageSquare size={15} /> },
      { id: "language" as SectionId, label: "Language",  icon: <Globe size={15} /> },
      { id: "tts" as SectionId,     label: "Voice", icon: <Volume2 size={15} /> },
    ] : []),
    // Voice / Face appear in continue mode only — they need the device's
    // hardware + backend, both unavailable while we're still on the AP. Both
    // are optional enrollments the operator can skip.
    ...(isContinue ? [
      { id: "voice" as SectionId, label: "My Voice", icon: <Mic size={15} />, optional: true },
      { id: "face"  as SectionId, label: "Face",     icon: <UserCircle size={15} />, optional: true },
    ] : []),
  ];

  // When the OS server pushed config, the operator only needs Device + Wi-Fi visible —
  // the rest are filled from URL and submitted silently. Sections remain in
  // the DOM (see `devicePushedConfig` display:none wrappers below) so values
  // still flow through the form; we just hide the menu entries.
  const visibleSections = devicePushedConfig
    ? SECTIONS.filter((s) => s.id === "wifi" || (isV1 && s.id === "device"))
    : SECTIONS;

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Per-step validation hint shown under the wizard buttons when Next is
  // blocked. Distinct from `error` (submit-level / backend errors) so the two
  // don't clobber each other. Cleared on any successful navigation.
  const [stepError, setStepError] = useState<string | null>(null);
  const [setupWorking, setSetupWorking] = useState(false);
  // Setup phase mirrors the backend SetupStatus enum: connecting → connected
  // (success path) or failed. Drives the post-submit screen UI.
  const [setupPhase, setSetupPhase] = useState<"connecting" | "connected" | "failed">("connecting");
  const [setupLanIP, setSetupLanIP] = useState<string>("");
  const [setupErrorMsg, setSetupErrorMsg] = useState<string>("");
  // Seconds elapsed since the join started, shown on the connecting screen so
  // the wait feels measured rather than open-ended. Reset + ticked by the
  // effect below whenever we enter the connecting phase.
  const [elapsed, setElapsed] = useState(0);
  // V1 starts on the Device step (admin password lives there). V2 is one step,
  // starting on Wi-Fi (which now hosts the admin password above the network).
  const [activeSection, setActiveSection] = useState<SectionId>(isV1 ? "device" : "wifi");
  const contentRef = useRef<HTMLDivElement>(null);

  const [deviceId, setDeviceId] = useState(urlParams.deviceId || "");
  const [mac, setMac] = useState("");
  // Admin password the operator picks here. Server bcrypts it into
  // config.admin_password_hash and uses it to gate browser admin access via
  // /api/login. V1 hosts it in the Device step (hidden, with a confirm field);
  // V2 hosts it in the Wi-Fi step (shown by default, no confirm). The confirm
  // state is only used by V1 — it stays harmlessly empty under V2.
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
  // mDNS hostname for the device on home Wi-Fi: `<device_type>-<suffix>.local`.
  // Matches what stage_ap sets via `hostnamectl set-hostname ${DEVICE_TYPE}-${SUFFIX_LC}`
  // — both sides derive from the device's hardware ID (Pi device-tree serial /
  // cpuinfo Serial / eth0 MAC) and the DEVICE_TYPE class, via the same logic in
  // os/services/internal/device/hardware.go.
  //
  // The backend returns `cfg.mac` already formatted as "<device_type>-XXXX" and
  // lowercase (see GetDeviceMac()), so the lowercased mac IS the hostname. Derive
  // the prefix from mac — never hardcode "lamp-" — so the redirect follows the
  // device class (lamp-a1b2 / intern-3c4d). The 4-char hex tail is validated to
  // guard against an empty/partial config. Empty when the config hasn't returned
  // yet, or when the device couldn't determine a serial.
  const deviceMdnsHost = useMemo(() => {
    const host = (mac || "").trim().toLowerCase();
    if (!/^[0-9a-f]{4}$/.test(host.slice(-4))) return "";
    return host;
  }, [mac]);
  // Device-type prefix ("lamp-" / "intern-") split off the same mac, used as the
  // router-admin search hint when the full mDNS host isn't usable yet. Follows
  // whatever device is actually being set up — never a hardcoded class.
  const deviceTypePrefix = useMemo(() => {
    const m = (mac || "").trim().toLowerCase();
    const dash = m.lastIndexOf("-");
    return dash > 0 ? m.slice(0, dash + 1) : "";
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
  // directly; only relevant in continue mode (device online).
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
    // device-section is "done" when a device id exists AND, if the device has
    // no admin password on file yet, the operator has filled + confirmed one.
    // Devices that already have a hash satisfy the gate automatically.
    // NOTE: do NOT gate on deviceId. It's a read-only, device-populated field
    // and in initial/AP mode GET /api/device/config is admin-gated (401), so it
    // arrives empty — the operator can't fill it. The only thing they actually
    // do here is set the admin password, so that's the gate. (Submit also
    // doesn't require deviceId — it merges server-side — so this keeps per-step
    // gating consistent with submit.)
    // V1: Device step is done when the device already has a password on file,
    //     or the operator typed one ≥ min chars AND it matches the confirm field.
    // V2: Device is not a step (merged into Wi-Fi) → always satisfied.
    device: isV1
      ? (hasAdminPassword || (adminPassword.length >= ADMIN_PASSWORD_MIN && adminPassword === adminPasswordConfirm))
      : true,
    // Wi-Fi step needs a network + a Wi-Fi password (or one on file). In V2 it
    // ALSO owns the admin password, so the admin requirement (on file, or
    // ≥ min chars typed) is folded in. V1 leaves the admin gate to the Device step.
    // Mirrors the submit-time preflight so per-step gating and final submit agree.
    wifi: !!ssid && (!!password || hasNetworkPassword)
      && (isV1 || hasAdminPassword || adminPassword.length >= ADMIN_PASSWORD_MIN),
    llm: !!llmApiKey || llmLoaded.apiKey,
    language: true, // Auto/empty is a valid choice — never block on this.
    realtime: true, // Not a setup step (edit-only card); never blocks setup.
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

  // Continue mode: refresh enrolled face/voice owners (device is online now).
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
    const required: SectionId[] = [
      ...(isV1 ? ["device" as SectionId] : []),
      "wifi", "llm", "channel", "tts", "voice", "face",
    ];
    // Redirect any time all required sections become done — including later
    // ticks when async data (e.g. faceOwners) arrives after first paint. This
    // path is NOT gated by autoScrolledRef on purpose; otherwise the first
    // effect run (before faceOwners loaded) sets the ref and the redirect
    // never fires once enrollment counts come back.
    if (required.every((id) => sectionDone[id])) {
      // Skip auto-bounce when user is on #force testing the UI on a
      // provisioned device, or when running on a local dev host pointed at a
      // remote device — they want to see the page, not jump away.
      if (autoScrolledRef.current && !forceHash && !isLocalDev) navigate("/monitor", { replace: true });
      return;
    }
    if (autoScrolledRef.current) return;
    const order: SectionId[] = [
      ...(isV1 ? ["device" as SectionId] : []),
      "wifi", "llm", "channel", "language", "tts", "voice", "face",
    ];
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
    setupWorking, setupLanIP,
    setSetupPhase, setSetupLanIP, setSetupErrorMsg,
  });

  // ── Parent-window event bridge (autonomous.ai) ──────────────────────────────
  // Notify whoever opened this popup of each meaningful Setup milestone via
  // postMessage (see lib/setupBridge.ts). All emits are best-effort no-ops when
  // there's no opener, so they never affect the flow. Each milestone is fired
  // from a focused effect so it tracks the real state transition exactly once.

  // Wizard mounted and ready.
  useEffect(() => {
    setupBridge.opened({ mode, deviceId, mac });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Operator moved to a new step.
  useEffect(() => { setupBridge.stepChanged(activeSection); }, [activeSection]);
  // A WiFi network was chosen.
  useEffect(() => { if (ssid) setupBridge.wifiSelected(ssid); }, [ssid]);
  // Validation / backend error surfaced.
  useEffect(() => { if (error) setupBridge.error(error); }, [error]);
  // Post-submit phase transitions: connecting → connected | failed.
  useEffect(() => {
    if (!setupWorking) return;
    if (setupPhase === "connecting") setupBridge.connecting();
    else if (setupPhase === "connected") setupBridge.connected({ mdns_host: deviceMdnsHost, lan_ip: setupLanIP });
    else if (setupPhase === "failed") setupBridge.failed(setupErrorMsg || "Wi-Fi setup failed.");
  }, [setupWorking, setupPhase, deviceMdnsHost, setupLanIP, setupErrorMsg]);


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
    setStepError(null); // moving to any section clears a stale per-step hint
    // Pop the content area back to the top so a Back/Next click never lands
    // the operator mid-scroll of the previous section.
    contentRef.current?.scrollTo({ top: 0 });
  };

  // Wizard-style step navigation: Prev/Next walk through visibleSections; the
  // submit button only renders on the last visible step. Auto-scroll edge
  // cases (activeSection on a hidden section) fall back to index 0 so Next
  // still advances into the visible set.
  const currentStepIndex = Math.max(0, visibleSections.findIndex((s) => s.id === activeSection));
  const currentStep = visibleSections[currentStepIndex];
  const isFirstStep = currentStepIndex === 0;
  const isLastStep = currentStepIndex >= visibleSections.length - 1;
  // On an optional step the operator hasn't completed yet, the forward action
  // reads "Skip" rather than "Next" so they understand they can move on without
  // enrolling. Once they've enrolled (sectionDone), it reverts to "Next".
  const isSkippableStep = !!currentStep?.optional && !sectionDone[currentStep.id];
  // Overall completion drives the sidebar + topbar progress bars. Counts only
  // the sections currently visible so the denominator matches what the
  // operator actually sees (e.g. ?debug, devicePushedConfig, continue mode).
  const doneCount = visibleSections.filter((s) => sectionDone[s.id]).length;
  const progressPct = visibleSections.length
    ? Math.round((doneCount / visibleSections.length) * 100)
    : 0;
  const goPrev = () => {
    setStepError(null);
    if (isFirstStep) return;
    scrollTo(visibleSections[currentStepIndex - 1].id);
  };
  // Per-step gate: a required step that isn't `sectionDone` blocks Next with an
  // inline, field-specific hint — far better than walking all the way to submit
  // and bouncing back several steps, and clearer than a silently-disabled
  // button (which never explains *why* it won't advance). Optional steps
  // (Voice/Face) always pass. Back never validates.
  const STEP_BLOCK_HINTS: Partial<Record<SectionId, string>> = {
    // V1 has a dedicated Device step that owns the admin-password hint; its
    // Wi-Fi hint stays about Wi-Fi only. V2 folds the admin password into the
    // Wi-Fi step, so its Wi-Fi hint also mentions the password when missing.
    ...(isV1
      ? {
          device: hasAdminPassword
            ? "Device info is still loading."
            : `Set an admin password (min ${ADMIN_PASSWORD_MIN} characters) and confirm it before continuing.`,
          wifi: "Choose a Wi-Fi network and enter its password before continuing.",
        }
      : {
          wifi: hasAdminPassword
            ? "Choose a Wi-Fi network and enter its password before continuing."
            : `Set a password (min ${ADMIN_PASSWORD_MIN} characters), then choose a Wi-Fi network and enter its password.`,
        }),
    llm: "Add the AI Brain API key before continuing.",
    channel: "Add the messaging channel token before continuing.",
  };
  const goNext = () => {
    if (isLastStep) return;
    if (currentStep && !currentStep.optional && !sectionDone[currentStep.id]) {
      setStepError(STEP_BLOCK_HINTS[currentStep.id] ?? "Complete this step before continuing.");
      return;
    }
    setStepError(null);
    scrollTo(visibleSections[currentStepIndex + 1].id);
  };

  const uniqueNetworks = useMemo(
    () => [...new Map(networks.filter((n) => n.ssid !== "").map((n) => [n.ssid, n])).values()],
    [networks],
  );

  // Elapsed-seconds ticker for the connecting screen. Runs only while we're in
  // the connecting phase; resets on entry so re-tries (failed → Back → submit)
  // start from zero. Cleared on unmount / phase change.
  useEffect(() => {
    if (!setupWorking || setupPhase !== "connecting") return;
    setElapsed(0);
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [setupWorking, setupPhase]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Require an admin password only when the device doesn't already have
    // one on file. Already-provisioned devices that pre-date the Login UI
    // batch land here with hasAdminPassword=false and must pick one now;
    // devices that have a hash skip the check entirely.
    if (!hasAdminPassword) {
      // The admin-password field lives on a different step per version, so
      // errors bounce the operator to the step that actually shows it.
      const pwStep: SectionId = isV1 ? "device" : "wifi";
      if (!adminPassword) {
        setError("Pick a password — you'll use it to sign in later.");
        setActiveSection(pwStep);
        return;
      }
      // The min-length floor is a frontend-only policy (the backend bcrypts any
      // non-empty value). This protects a device with a camera/mic from a
      // trivially guessable admin login.
      if (adminPassword.length < ADMIN_PASSWORD_MIN) {
        setError(`Password must be at least ${ADMIN_PASSWORD_MIN} characters.`);
        setActiveSection(pwStep);
        return;
      }
      // V1 only: a confirm field exists, so it must match. V2 shows the
      // password in clear text and drops confirm entirely.
      if (isV1 && adminPassword !== adminPasswordConfirm) {
        setError("Admin password and confirmation don't match.");
        setActiveSection(pwStep);
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
      setupBridge.submitted({ ssid: ssid.trim(), channel });
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
      {/* ── Sidebar (hidden on mobile) ── */}
      <aside className="lm-sidebar" style={{
        width: 192, flexShrink: 0,
        background: C.sidebar, borderRight: `1px solid ${C.border}`,
        display: "flex", flexDirection: "column",
      }}>

        {/* Brand header + overall progress so the operator always sees how far
            they are into the wizard from the sidebar. */}
        <div style={{ padding: "16px 16px 12px" }}>
          <div style={{ fontSize: 14.5, fontWeight: 700, color: C.text, letterSpacing: "0.01em" }}>
            Device Setup
          </div>
          <div style={{ fontSize: 12, color: C.textMuted, marginTop: 3 }}>
            {doneCount} of {visibleSections.length} done
          </div>
          <div className="lm-progress-track" style={{ marginTop: 10 }}>
            <div className="lm-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
        </div>

        <nav style={{ padding: "4px 0 10px", flex: 1 }}>
          {visibleSections.map((s) => {
            const active = activeSection === s.id;
            // Show checks whenever a section's value is filled — including in
            // #force (initial) mode if the device already has saved config to
            // prefill from. A truly empty device still shows zero checks
            // because sectionDone returns false across the board.
            const done = sectionDone[s.id];
            return (
              <button
                key={s.id}
                onClick={() => scrollTo(s.id)}
                className={`lm-nav-item${active ? " lm-nav-item--active" : ""}${done && !active ? " lm-nav-item--done" : ""}`}
              >
                {s.icon}
                <span style={{ flex: 1 }}>{s.label}</span>
                {s.optional && !done && (
                  <span style={{
                    fontSize: 10, fontWeight: 600, color: C.textMuted,
                    textTransform: "uppercase", letterSpacing: "0.04em",
                  }}>
                    Optional
                  </span>
                )}
                {done && <Check size={14} className="lm-pop" style={{ color: C.green }} />}
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

        {/* Mobile tabs (hidden on desktop). The theme toggle sits OUTSIDE the
            horizontally-scrolling tab row so it stays pinned (margin-left:auto
            doesn't pin inside an overflow-x flex container) and isn't hidden
            under the right-edge scroll-hint fade.
            Hidden entirely for a single-step flow (e.g. device-pushed config
            collapses the wizard to just Wi-Fi) — a lone tab chip is noise and
            looked like a stray label in the companion-app popup. */}
        {visibleSections.length > 1 && (
        <div className="lm-mobile-tabs-wrap" style={{
          display: "none", flexShrink: 0,
          borderBottom: `1px solid ${C.border}`,
          alignItems: "center", gap: 4, padding: "8px 8px 8px 12px",
        }}>
          <div className="lm-mobile-tabs lm-hide-scroll" style={{
            display: "flex", overflowX: "auto", gap: 4, flex: 1, alignItems: "center",
          }}>
            {visibleSections.map((s) => {
              const active = activeSection === s.id;
              return (
                <button
                  key={s.id}
                  onClick={() => scrollTo(s.id)}
                  className={`lm-tab${active ? " lm-tab--active" : ""}`}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 6px", flexShrink: 0,
          }}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>
        )}

        {/* Topbar */}
        <div style={{ borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
          <div style={{
            padding: "12px 24px 10px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <span style={{ fontSize: 15, fontWeight: 600, color: C.text }}>
              {setupWorking ? "Setting up…" : SECTIONS.find((s) => s.id === activeSection)?.label ?? "Wi-Fi"}
            </span>
            {/* "Step X / Y" only makes sense for a multi-step wizard. With a
                single visible step (V2's merged flow) it's noise, so hide it. */}
            {!setupWorking && visibleSections.length > 1 && (
              <span style={{ fontSize: 12, color: C.textDim }}>
                Step {currentStepIndex + 1} / {visibleSections.length}
              </span>
            )}
          </div>
          {/* Per-step progress mirrors the wizard position (not section-done
              count) so the bar advances as the operator walks Back/Next.
              Hidden for a single-step flow — there's no progress to show when
              Wi-Fi is the only step (device-pushed config). */}
          {!setupWorking && visibleSections.length > 1 && (
            <div className="lm-progress-track" style={{ borderRadius: 0 }}>
              <div
                className="lm-progress-fill"
                style={{
                  borderRadius: 0,
                  width: `${((currentStepIndex + 1) / Math.max(1, visibleSections.length)) * 100}%`,
                }}
              />
            </div>
          )}
        </div>

        {/* Content */}
        <div ref={contentRef} className="lm-fade-in lm-main-content" style={{
          flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px",
        }}>
          <div style={{ maxWidth: 560, margin: "0 auto" }}>

            {/* Post-submit screen: shows progress while the device joins
                Wi-Fi, then a QR + IP for the user to continue setup on the
                home network once the AP shuts down. */}
            {setupWorking ? (
              <div className="lm-card lm-fade-in" style={{
                padding: "32px 24px", textAlign: "center",
              }}>
                {setupPhase === "connecting" && (
                  <>
                    <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
                      <span className="lm-wifi-pulse" aria-hidden>
                        <span className="lm-wifi-ring" />
                        <span className="lm-wifi-ring lm-r2" />
                        <span className="lm-wifi-ring lm-r3" />
                        <span className="lm-wifi-icon">
                          <Wifi size={26} strokeWidth={2} />
                        </span>
                      </span>
                    </div>
                    <div style={{ fontSize: 14.5, fontWeight: 600, color: C.amber, marginBottom: 8 }}>
                      Your device is joining Wi-Fi
                      <span className="lm-blink">.</span><span>.</span><span>.</span>
                    </div>
                    <div style={{ fontSize: 13, color: C.textDim, marginBottom: 14, lineHeight: 1.5 }}>
                      This usually takes 10-30 seconds. Stay on this network.
                    </div>
                    {/* Indeterminate progress + elapsed counter: the join has no
                        knowable %, so a sweeping bar signals "working" while the
                        seconds give the wait a measured feel. */}
                    <div className="lm-indeterminate" style={{ marginBottom: 7 }} />
                    <div style={{ fontSize: 11, color: C.textMuted, marginBottom: setupLanIP ? 18 : 0 }}>
                      Elapsed {elapsed}s
                    </div>
                    {/* Surface the raw-IP address NOW, while we still have a
                        connection — once the operator switches to home Wi-Fi
                        this page goes away and an un-copied address is lost.
                        The auto-redirect on "connected" still handles the happy
                        path; this is the safety net for when it doesn't land
                        (AP dropping before the phase poll flips).
                        IP-only by design: the .local name is unreliable on
                        mDNS-blocking networks, so we show nothing until the
                        backend's early-capture poll hands us a LAN IP.
                        Toned down here vs. the "connected" screen: a single
                        compact label + the copy field, no long paragraph, so it
                        stays a quiet safety net rather than competing with the
                        primary "joining…" message. */}
                    {setupLanIP && (
                      <div style={{
                        marginTop: 4, paddingTop: 14,
                        borderTop: `1px solid ${C.border}`,
                        textAlign: "left",
                      }}>
                        <div style={{
                          fontSize: 13, color: C.textDim, marginBottom: 6, lineHeight: 1.5,
                        }}>
                          This page disconnects when you rejoin home Wi-Fi — save
                          this address to continue:
                        </div>
                        <CopyAddress url={`http://${setupLanIP}/setup`} />
                      </div>
                    )}
                  </>
                )}

                {setupPhase === "connected" && (
                  <>
                    <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
                      <CheckCircle2 size={34} color={C.green} strokeWidth={1.75} aria-hidden />
                    </div>
                    <div style={{ fontSize: 14.5, fontWeight: 600, color: C.amber, marginBottom: 16 }}>
                      Your device is online!
                    </div>

                    {/* IP path (only path): we redirect to the device's raw LAN
                        IP, never its `.local` mDNS name — `.local` is unreliable
                        on mDNS-blocking routers, whereas an IP resolves on every
                        network. Shown once the backend's early-capture poll has
                        handed us a LAN IP; otherwise we fall back to a
                        router-admin hint so the operator can find the IP. */}
                    {setupLanIP ? (
                      <>
                        {/* Action-first ordering: the one thing the user must do
                            now (rejoin home Wi-Fi, then Continue) leads, with the
                            primary button right under it. The IP address + router
                            fallback drop below a divider as a quiet safety-net for
                            when auto-redirect/Continue doesn't land — mirroring
                            the connecting screen's hierarchy. */}
                        <div style={{ fontSize: 13, color: C.textDim, marginBottom: 16, lineHeight: 1.5 }}>
                          Reconnect your computer to your home Wi-Fi, then click
                          Continue.
                        </div>
                        <a
                          // Carry the current pathname + query params so any
                          // ?llm_api_key=… etc. from the OS server remain in scope on
                          // the new host (redundant — the OS server already persisted
                          // them via submit — but cheap and useful when the
                          // operator re-runs setup with different overrides).
                          // Force reload when the user is already on the device's
                          // IP — otherwise the browser no-ops the same-URL click
                          // and they stay stuck on the "Your device is online!"
                          // screen even though the device is reachable in continue
                          // mode now.
                          href={`http://${setupLanIP}${window.location.pathname}${getInitialSearch()}`}
                          onClick={(e) => {
                            setupBridge.continueClicked({ mdns_host: deviceMdnsHost });
                            if (window.location.hostname === setupLanIP) {
                              e.preventDefault();
                              window.location.reload();
                            }
                          }}
                          className="lm-btn lm-btn-primary"
                          style={{
                            display: "inline-block", padding: "10px 22px",
                            textDecoration: "none",
                          }}
                        >
                          Continue setup →
                        </a>
                        {/* Safety-net block: divider + the IP address and a
                            router-admin hint, toned down so it doesn't compete
                            with the Continue button above. */}
                        <div style={{
                          marginTop: 18, paddingTop: 16,
                          borderTop: `1px solid ${C.border}`, textAlign: "left",
                        }}>
                          <div style={{ fontSize: 13, color: C.textDim, marginBottom: 6, lineHeight: 1.5 }}>
                            Or open this address once you're back on home Wi-Fi:
                          </div>
                          <CopyAddress url={`http://${setupLanIP}/setup`} />
                          <div style={{ fontSize: 12, color: C.textMuted, marginTop: 8, lineHeight: 1.5 }}>
                            Can't reach it? Find your device's IP in your router's
                            admin page{deviceTypePrefix ? ` (look for "${deviceTypePrefix}")` : ""}.
                          </div>
                        </div>
                      </>
                    ) : (
                      <div style={{ fontSize: 13, color: C.textDim, lineHeight: 1.5 }}>
                        Your device is connected. Open your router's admin page to find
                        the device's IP address{deviceTypePrefix ? ` (look for "${deviceTypePrefix}")` : ""}.
                      </div>
                    )}
                  </>
                )}

                {setupPhase === "failed" && (
                  <>
                    <div style={{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
                      <XCircle size={34} color={C.red} strokeWidth={1.75} aria-hidden />
                    </div>
                    <div style={{ fontSize: 14.5, fontWeight: 600, color: C.red, marginBottom: 8 }}>
                      Wi-Fi setup failed
                    </div>
                    <div style={{ fontSize: 13, color: C.textDim, marginBottom: 16, lineHeight: 1.5 }}>
                      {setupErrorMsg || "Couldn't connect to the network you chose."}
                    </div>

                    {/* Actionable checklist. Wi-Fi join failures on these
                        devices are overwhelmingly one of these three causes, so
                        we spell them out instead of a generic "try again" —
                        the 2.4GHz one in particular is non-obvious to most
                        people and the single most common cause. */}
                    <div style={{
                      textAlign: "left", background: C.surface,
                      border: `1px solid ${C.border}`, borderRadius: 8,
                      padding: "12px 14px", marginBottom: 18, fontSize: 13,
                      color: C.textDim, lineHeight: 1.6,
                    }}>
                      <div style={{ fontWeight: 600, color: C.text, marginBottom: 6 }}>
                        Things to check:
                      </div>
                      <div>• Use a <strong style={{ color: C.text }}>2.4GHz</strong> Wi-Fi network — most devices can't join 5GHz.</div>
                      <div>• Double-check the Wi-Fi password (it's case-sensitive).</div>
                      <div>• Keep the device close to your router during setup.</div>
                    </div>

                    <button
                      type="button"
                      className="lm-btn lm-btn-primary"
                      onClick={() => { setupBridge.retryClicked(); setSetupWorking(false); setSetupPhase("connecting"); setActiveSection("wifi"); }}
                      style={{ padding: "9px 18px" }}
                    >
                      Back to Wi-Fi
                    </button>
                  </>
                )}
              </div>
            ) : (
              <>
                {error && (
                  <div className="lm-fade-in" style={{
                    background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
                    borderRadius: 8, padding: "10px 14px", fontSize: 12, color: C.red, marginBottom: 16,
                  }}>
                    {error}
                  </div>
                )}

                <form id="setup-form" onSubmit={handleSubmit} noValidate>

                  {/* V1: DeviceSection is a real step, hosting the admin password
                      (+ confirm) when the device has none on file. V2: it's kept
                      mounted but hidden — Device ID / MAC still flow through
                      submit, while the admin password moves into WifiSection. */}
                  <div style={isV1 ? undefined : { display: "none" }}>
                    <DeviceSection
                      active={isV1 && activeSection === "device"}
                      deviceId={deviceId} setDeviceId={setDeviceId}
                      mac={mac}
                      {...(isV1 && !hasAdminPassword ? {
                        adminPassword,
                        setAdminPassword,
                        adminPasswordConfirm,
                        setAdminPasswordConfirm,
                      } : {})}
                    />
                  </div>

                  <WifiSection
                    active={activeSection === "wifi"}
                    ssid={ssid} setSsid={setSsid}
                    password={password} setPassword={setPassword}
                    passwordConfigured={hasNetworkPassword && !password}
                    loadingList={loadingList}
                    uniqueNetworks={uniqueNetworks}
                    {...(!isV1 && !hasAdminPassword ? {
                      adminPassword,
                      setAdminPassword,
                    } : {})}
                  />

                  {/* When devicePushedConfig is on, the four sections below are
                      kept mounted but visually hidden — their state autofills
                      from URL params and still flows through the form submit. */}
                  <div style={devicePushedConfig ? { display: "none" } : undefined}>
                    <LLMSection
                      active={devicePushedConfig || activeSection === "llm"}
                      llmLoaded={llmLoaded}
                      llmApiKey={llmApiKey} setLlmApiKey={setLlmApiKey}
                      llmUrl={llmUrl} setLlmUrl={setLlmUrl}
                      llmModel={llmModel} setLlmModel={setLlmModel}
                    />

                    <ChannelSection
                      active={devicePushedConfig || activeSection === "channel"}
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
                      active={devicePushedConfig || activeSection === "language"}
                      sttLanguage={sttLanguage} setSttLanguage={setSttLanguage}
                    />

                    <TTSSection
                      active={devicePushedConfig || activeSection === "tts"}
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

                  {stepError && (
                    <div className="lm-fade-in" style={{
                      fontSize: 12, color: C.red, marginBottom: 10,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <span aria-hidden>⚠</span>{stepError}
                    </div>
                  )}

                  <div style={{
                    display: "flex", gap: 10, justifyContent: "space-between",
                    alignItems: "center", marginTop: 8,
                  }}>
                    {isFirstStep ? <span /> : (
                      <button
                        type="button"
                        onClick={goPrev}
                        className="lm-btn lm-btn-ghost"
                        style={{ padding: "9px 18px", fontWeight: 500 }}
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
                          onClick={() => { setupBridge.monitorClicked(); navigate("/monitor"); }}
                          className="lm-btn lm-btn-primary"
                          style={{ padding: "9px 22px" }}
                        >
                          {isSkippableStep ? "Skip & finish →" : "Go to monitor →"}
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
                          className="lm-btn lm-btn-primary"
                          style={{ padding: "9px 22px" }}
                        >
                          {loading ? "Setting up…" : "Setup"}
                        </button>
                      )
                    ) : (
                      <button
                        key="next"
                        type="button"
                        onClick={goNext}
                        className="lm-btn lm-btn-primary"
                        style={{ padding: "9px 22px" }}
                      >
                        {isSkippableStep ? "Skip →" : "Next →"}
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
