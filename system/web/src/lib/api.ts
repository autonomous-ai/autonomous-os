import camelcaseKeys from "camelcase-keys";
import type { NetworkItem, SetupRequest } from "@/types";

const API_BASE =
  import.meta.env.VITE_API_BASE ??
  import.meta.env.VITE_NETWORK_API ??
  import.meta.env.VITE_API_URL ??
  "";

/** 0 = error, 1 = success (matches backend JSONReponseStatus) */
export type JSONResponseStatus = 0 | 1;

export interface JSONResponse<T = unknown> {
  status: JSONResponseStatus;
  message: string | null;
  data: T;
}

// Legacy Bearer fallback. Browsers normally authenticate via the
// `os_session` cookie set by POST /api/login, but scripted callers and
// shareable dev links may still pass an explicit token. Cleared on logout;
// not persisted on first load (cookie auth makes sessionStorage unnecessary).
const TOKEN_STORAGE_KEY = "device_api_token";
let apiToken: string =
  typeof window !== "undefined" ? sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "" : "";

export function setApiToken(token: string): void {
  apiToken = token ?? "";
  if (typeof window === "undefined") return;
  if (apiToken) sessionStorage.setItem(TOKEN_STORAGE_KEY, apiToken);
  else sessionStorage.removeItem(TOKEN_STORAGE_KEY);
}

export function getApiToken(): string {
  return apiToken;
}

/** Append ?token=<key> to a URL only when a legacy Bearer token is in play.
 *  After login, cookies attach automatically — callers can pass URLs through
 *  this helper unchanged and the URL stays clean. */
export function withApiToken(url: string): string {
  if (!apiToken) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(apiToken)}`;
}

/** Build a `/api/hardware/<path>` URL. Cookie auto-attaches for same-origin
 *  requests, so this is now just a prefix builder — no token leaks into the
 *  URL, DOM, or browser history. Legacy Bearer fallback still rides along
 *  when a token is set (dev/scripted callers). */
export function hwUrl(path: string): string {
  return withApiToken(`/api/hardware${path}`);
}

/** Build a `GET /api/agent/file` URL for a DEVICE-LOCAL path the agent named in
 *  a reply (a camera snapshot, a generated report). The path is validated
 *  server-side against an allow-list of roots and served types — this helper
 *  only builds the URL, it makes no claim that the file is servable, so callers
 *  must handle 403/404 (an <img> onError, a link that just fails). */
export function agentFileUrl(devicePath: string): string {
  return withApiToken(`${API_BASE}/api/agent/file?path=${encodeURIComponent(devicePath)}`);
}

/** Base64-encode a File for JSON bodies (e.g. face enroll). Uses FileReader
 *  instead of `btoa(String.fromCharCode(...new Uint8Array(buf)))`: spreading a
 *  full-resolution JPEG's bytes into a function call blows the call stack
 *  (RangeError) on large photos, silently failing the upload. Strips the
 *  `data:<mime>;base64,` prefix so the result is the raw base64 HAL expects. */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve((reader.result as string).split(",")[1]);
    reader.onerror = () => reject(new Error("Failed to read file"));
    reader.readAsDataURL(file);
  });
}

// Setup query params that may carry secrets. When a redirect or shareable
// link preserves window.location.search, these must be stripped so the
// token doesn't propagate to a new origin, browser history, proxy log, or
// any clipboard the user pastes the URL into.
const SECRET_QUERY_KEYS = [
  "tele_token",
  "slack_bot_token",
  "slack_app_token",
  "discord_bot_token",
  "llm_api_key",
  "deepgram_api_key",
  "stt_api_key",
  "tts_api_key",
  "mqtt_password",
  "password",
  "admin_password",
];

/** Return window.location.search (or the given query string) with every
 *  known secret key removed. Preserves harmless params like `debug=true`. */
export function safeSearch(search?: string): string {
  const raw = search ?? (typeof window !== "undefined" ? window.location.search : "");
  if (!raw) return "";
  const p = new URLSearchParams(raw);
  let changed = false;
  for (const k of SECRET_QUERY_KEYS) {
    if (p.has(k)) {
      p.delete(k);
      changed = true;
    }
  }
  if (!changed) return raw;
  const out = p.toString();
  return out ? `?${out}` : "";
}

// Session-storage key that outlives the scrub. Setup's useSetupUrlParams
// module reads from this key when window.location.search is empty (post-scrub
// reload), so the Setup form can still ship the operator-provided secrets. Key
// MUST match the one in hooks/setup/useSetupUrlParams.ts.
const SETUP_URL_SEARCH_STORE_KEY = "autonomous.setup_url_search.v1";

/** Scrub secret query params from window.location without a navigation.
 *  Called once on every page mount so a `?llm_api_key=…` link doesn't survive
 *  in browser history / address bar / clipboard after the page reads it.
 *
 *  EXCEPTION — the /setup route is deliberately left untouched: the operator
 *  flow requires that an F5 on Setup keeps the full URL (secrets included) on
 *  the address bar, so a reload re-reads them straight from the query string
 *  rather than depending on sessionStorage rehydration (which does not survive
 *  the AP→STA origin change: 192.168.100.1 → the device's LAN IP). This is an
 *  accepted trade-off: secrets stay visible in Setup's history / address bar.
 *
 *  F5-reload survival (all OTHER routes except Login): persist the raw pre-scrub search to
 *  sessionStorage BEFORE wiping the URL. That way a reload (which reloads the
 *  scrubbed URL, losing everything the module-load snapshot in useSetupUrlParams
 *  would have captured) can still rehydrate the operator's secrets.
 *  sessionStorage is per-tab and cleared on tab close, so this stays a safer
 *  resting place than the URL — not shown in the address bar, not
 *  screenshot-captured, not walked by "back" history. Doing this here (rather
 *  than only in useSetupUrlParams) covers the cache-transitional case: a cached
 *  OLD JS bundle that runs scrub before the NEW JS bundle has ever loaded still
 *  seeds sessionStorage, so a subsequent F5 into NEW JS can rehydrate. */
export function scrubLocationSecrets(): void {
  if (typeof window === "undefined") return;
  // Keep the full URL (secrets included) on /setup — see doc comment above.
  if (window.location.pathname === "/setup") return;
  const raw = window.location.search;
  const cleaned = safeSearch(raw);
  if (cleaned === raw) return;
  try {
    // /login reads ?password during its first render and submits it straight
    // away. Do not retain that credential in sessionStorage after scrubbing.
    if (raw && window.location.pathname !== "/login") {
      sessionStorage.setItem(SETUP_URL_SEARCH_STORE_KEY, raw);
    }
  } catch {
    /* private-mode / storage disabled — URL scrub still proceeds */
  }
  const next = `${window.location.pathname}${cleaned}${window.location.hash}`;
  window.history.replaceState(null, "", next);
}

// Patched window.fetch: ensures every same-origin /api/* request rides the
// session cookie (credentials: include) and attaches a legacy Bearer header
// when one is in play. Browsers default fetch to credentials: 'same-origin'
// for same-origin requests, but Vite's dev server can confuse the heuristic
// and the explicit setting is cheap insurance.
if (typeof window !== "undefined" && !(window as unknown as { __osFetchPatched?: boolean }).__osFetchPatched) {
  const origFetch = window.fetch.bind(window);
  window.fetch = function patchedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    let url = "";
    if (typeof input === "string") url = input;
    else if (input instanceof URL) url = input.toString();
    else url = (input as Request).url;

    const isApiCall = url.startsWith("/api/") || url.includes("/api/");
    if (!isApiCall) return origFetch(input, init);

    // `mode: "no-cors"` fetches (the mDNS probe in useSetupStatusPolling is
    // the only intentional caller) must stay as the operator wrote them —
    // both the Authorization header (not in the CORS safelist) and the
    // forced `credentials: "include"` flip Chrome into preflight / private-
    // network restriction behaviour that throws before the request leaves
    // the page. Pass-through preserves the original "send raw ping, don't
    // care about response body" semantics.
    if (init?.mode === "no-cors") return origFetch(input, init);

    const headers = new Headers(init?.headers);
    if (apiToken && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${apiToken}`);
    }
    return origFetch(input, { ...init, headers, credentials: "include" });
  };
  (window as unknown as { __osFetchPatched?: boolean }).__osFetchPatched = true;
}

async function apiRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (apiToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }
  const res = await fetch(url, { credentials: "include", ...options, headers });
  const json = (await res.json()) as JSONResponse<T>;
  if (json.status !== 1) {
    const msg =
      typeof json.message === "string" ? json.message : res.ok ? "Request failed" : res.statusText;
    const err = new Error(msg) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return json.data;
}

/**
 * Converts object keys from snake_case to camelCase (uses camelcase-keys).
 * Use for API responses that return snake_case keys.
 */
export function parseSnakeToCamel<T = Record<string, unknown>>(
  raw: Record<string, unknown>,
  options?: { deep?: boolean }
): T {
  return camelcaseKeys(raw as Record<string, unknown>, { deep: options?.deep ?? false }) as T;
}

export async function getNetworks(): Promise<NetworkItem[]> {
  return apiRequest<NetworkItem[]>(`${API_BASE}/api/network`);
}

/** The Wi-Fi network wlan0 is currently associated with (from `iwgetid -r`),
 *  or null when the interface isn't associated with any station network.
 *  Public (no admin auth) so the reloaded Setup page — served from the new
 *  LAN IP after the AP→STA join — can confirm the device is actually on home
 *  Wi-Fi and mark the Wi-Fi step done without reading admin-gated config. */
export interface CurrentNetwork {
  ssid: string;
  signal: number;
  linkRate: number;
}

/** GET /api/network/current — the SSID the device is presently joined to.
 *  Returns null when wlan0 isn't associated (e.g. still running the setup AP). */
export async function getCurrentNetwork(): Promise<CurrentNetwork | null> {
  return apiRequest<CurrentNetwork | null>(`${API_BASE}/api/network/current`);
}

export async function setupNetwork(ssid: string, password: string): Promise<string> {
  return apiRequest<string>(`${API_BASE}/api/network/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssid, password }),
  });
}

export async function setupDevice(body: SetupRequest): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** POST /api/device/wifi-provision — AP-portal setup path. `ssid` is
 *  required; every other field is optional and, when omitted or empty, tells
 *  the backend to leave the current on-disk value alone. Backend gates this
 *  to callers on the AP subnet (192.168.100.0/24), so it only works when the
 *  browser is joined to the device's own hotspot. Used by the standalone
 *  /wifi page (not the full /setup wizard). */
export interface WifiProvisionBody {
  ssid: string;
  password?: string;
  // LLM
  llm_api_key?: string;
  llm_base_url?: string;
  llm_model?: string;
  // Voice pipeline
  stt_api_key?: string;
  stt_base_url?: string;
  stt_language?: string;
  tts_api_key?: string;
  tts_base_url?: string;
  tts_provider?: string;
  tts_voice?: string;
  // Admin auth
  admin_password?: string;
  // Messaging channel (optional). channel = telegram | slack | discord.
  // Only the sub-tokens matching `channel` are honored by the backend.
  channel?: string;
  telegram_bot_token?: string;
  telegram_user_id?: string;
  slack_bot_token?: string;
  slack_app_token?: string;
  slack_user_id?: string;
  discord_bot_token?: string;
  discord_guild_id?: string;
  discord_user_id?: string;
}
export async function wifiProvision(body: WifiProvisionBody): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/wifi-provision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface SetupStatus {
  phase: "idle" | "connecting" | "connected" | "failed";
  lan_ip: string;
  error: string;
  // Hardware-derived "Lamp-XXXX". Used by the web client to compute the
  // canonical mDNS hostname (`lamp-xxxx.local`) for the AP→STA auto-redirect.
  // Exposed on this open endpoint because /api/device/config is admin-gated
  // and fresh devices have no admin yet.
  mac: string;
  // Setup runs since the device booted, bumped when a run starts. Lets the
  // poller recognise its own run's verdict without having to catch the
  // "connecting" phase live — see useSetupStatusPolling. Optional: a device on
  // an older os-server build simply omits it.
  run?: number;
  // Whether the device has ever completed setup — what decides the initial
  // wizard vs the continue wizard (`SetupGate`). Optional for the same
  // older-build reason; absent falls back to the internet heuristic.
  set_up_completed?: boolean;
}

/** Polled by Setup.tsx during the AP→STA transition. Returns the device's
 *  current setup phase plus the LAN IP once Wi-Fi is associated, so the web
 *  client can redirect the user to the new URL. */
export async function getSetupStatus(): Promise<SetupStatus> {
  return apiRequest<SetupStatus>(`${API_BASE}/api/device/setup/status`);
}

export async function checkInternet(): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/network/check-internet`);
}


export async function getSetup(): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/setup`);
}

/** Sanitized device config — Has* booleans replace raw secrets so they
 *  never reach the DOM / sessionStorage / HAR captures. PUT
 *  /api/device/config still accepts plaintext writes through SecretUpdateField. */
export interface DeviceConfig {
  channel: string;
  telegram_user_id: string;
  slack_user_id: string;
  discord_guild_id: string;
  discord_user_id: string;
  llm_model: string;
  llm_base_url: string;
  llm_disable_thinking: boolean;
  stt_base_url: string;
  tts_base_url: string;
  stt_language: string;
  stt_model: string;
  tts_provider: string;
  tts_voice: string;
  wakeword: boolean;
  agent_name: string;
  wake_phrases: string[];
  realtime?: {
    enabled?: boolean;
    provider?: string;
    model?: string;
    voice?: string;
    reasoning?: string;
    base_url?: string;
    has_api_key?: boolean;
  };
  device_id: string;
  mac: string;
  network_ssid: string;
  mqtt_endpoint: string;
  mqtt_username: string;
  mqtt_port: number;
  fa_channel: string;
  fd_channel: string;

  has_telegram_bot_token: boolean;
  has_slack_bot_token: boolean;
  has_slack_app_token: boolean;
  has_discord_bot_token: boolean;
  has_llm_api_key: boolean;
  has_deepgram_api_key: boolean;
  has_stt_api_key: boolean;
  has_tts_api_key: boolean;
  has_network_password: boolean;
  has_mqtt_password: boolean;
  has_admin_password: boolean;
}

export async function getTTSVoices(provider?: string, lang?: string): Promise<string[]> {
  const qs = new URLSearchParams();
  if (provider) qs.set("provider", provider);
  if (lang) qs.set("lang", lang);
  const params = qs.toString() ? `?${qs.toString()}` : "";
  return apiRequest<string[]>(`${API_BASE}/api/device/voices${params}`);
}

export async function getTTSProviders(): Promise<string[]> {
  return apiRequest<string[]>(`${API_BASE}/api/device/tts-providers`);
}

export interface RealtimeOptions {
  providers: string[];
  voices: Record<string, string[]>;
  reasoning: Record<string, string[]>;
}

export async function getRealtimeOptions(): Promise<RealtimeOptions> {
  return apiRequest<RealtimeOptions>(`${API_BASE}/api/device/realtime-options`);
}

export interface AgentRuntimeStatus {
  current: string;
  options: string[];
}

export async function getAgentRuntime(): Promise<AgentRuntimeStatus> {
  return apiRequest<AgentRuntimeStatus>(`${API_BASE}/api/device/agent-runtime`);
}

/** POST /api/device/agent-runtime — swap the agentic backend (openclaw ⇄ hermes).
 *  The device restarts os-server right after, so the connection drops; callers
 *  should treat success as "accepted, reconnecting" and re-poll once it's back. */
export async function setAgentRuntime(runtime: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/agent-runtime`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runtime }),
  });
}

export interface TimezoneStatus {
  current: string;
  zones: string[];
}

/** GET /api/device/timezone — current IANA zone + selectable list (from system tzdata). */
export async function getTimezone(): Promise<TimezoneStatus> {
  return apiRequest<TimezoneStatus>(`${API_BASE}/api/device/timezone`);
}

/** POST /api/device/timezone — apply an IANA zone (e.g. "Asia/Ho_Chi_Minh").
 *  Writes /etc/localtime + /etc/timezone and persists to config; takes effect
 *  without a HAL restart (clock helpers read /etc/timezone live). */
export async function setTimezone(timezone: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/timezone`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone }),
  });
}

export interface TestTTSOptions {
  text?: string;
  /** BCP-47 stt_language code; picks a friendly demo phrase in that language. */
  lang?: string;
  provider?: string;
  /** Optional URL/key override — lets the admin's Test Voice validate a
   *  pending edit BEFORE clicking Save Changes. Empty = server falls back
   *  to the on-disk config. Same-origin admin call, so echoing the key
   *  back adds no exposure vs storing it on the device. */
  baseUrl?: string;
  apiKey?: string;
}

const TTS_DEMO_PHRASES: Record<string, string> = {
  en: "[laugh] Hey! How are you doing today?",
  vi: "[laugh] Chào bạn, hôm nay bạn thế nào?",
  "zh-CN": "[laugh] 嗨，你今天怎么样？",
  "zh-TW": "[laugh] 嗨，你今天怎麼樣？",
};

function demoPhraseFor(lang?: string): string {
  if (!lang) return TTS_DEMO_PHRASES.en;
  return TTS_DEMO_PHRASES[lang] || TTS_DEMO_PHRASES.en;
}

/** POST /api/voice/preview — server reads the TTS API key + base URL from
 *  cfg by default, or from the optional baseUrl/apiKey overrides in opts
 *  (so the admin's Test Voice can validate a pending edit before Save).
 *  Same-origin + adminAuth gated. */
export async function testTTSVoice(voice: string, opts: TestTTSOptions = {}): Promise<void> {
  await apiRequest<boolean>(`${API_BASE}/api/voice/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text: opts.text || demoPhraseFor(opts.lang),
      voice,
      provider: opts.provider || undefined,
      base_url: opts.baseUrl || undefined,
      api_key: opts.apiKey || undefined,
    }),
  });
}

export async function getDeviceConfig(): Promise<DeviceConfig> {
  return apiRequest<DeviceConfig>(`${API_BASE}/api/device/config`);
}

export async function updateDeviceConfig(body: Partial<Record<string, unknown>>): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** POST /api/login — server validates bcrypt(password) against
 *  config.AdminPasswordHash and sets the os_session cookie on success. */
export async function login(password: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}

// MCP Tools — remote MCP tool endpoints (HF Spaces, public MCP servers).
// headers is optional; key-value pairs sent with every MCP request.
export interface MCPTool { name: string; url: string; headers?: Record<string, string> }

/** GET /api/device/mcp-tools */
export async function listMCPTools(): Promise<MCPTool[]> {
  return apiRequest<MCPTool[]>(`${API_BASE}/api/device/mcp-tools`);
}

/** POST /api/device/mcp-tools */
export async function addMCPTool(tool: MCPTool): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/mcp-tools`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tool),
  });
}

/** DELETE /api/device/mcp-tools/:name */
export async function removeMCPTool(name: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/device/mcp-tools/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// Plugins — standalone Python apps installed from git URLs.
export interface Plugin { name: string; version: string; description: string; status: string; url: string }

/** GET /api/plugin */
export async function listPlugins(): Promise<Plugin[]> {
  return apiRequest<Plugin[]>(`${API_BASE}/api/plugin`);
}

/** POST /api/plugin/install */
export async function installPlugin(url: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/plugin/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

/** POST /api/plugin/:name/start */
export async function startPlugin(name: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/plugin/${encodeURIComponent(name)}/start`, {
    method: "POST",
  });
}

/** POST /api/plugin/:name/stop */
export async function stopPlugin(name: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/plugin/${encodeURIComponent(name)}/stop`, {
    method: "POST",
  });
}

/** DELETE /api/plugin/:name */
export async function uninstallPlugin(name: string): Promise<boolean> {
  return apiRequest<boolean>(`${API_BASE}/api/plugin/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// HuggingFace plugin discovery — PARKED, not deleted (#213). Plugins move to
// our own catalog, beside skills. Restore this pair together with the Go
// handler and its route, and point the request at the catalog endpoint; the
// StoreSkill client just below is the shape to copy.
//
// export interface HFSpace {
//   id: string;
//   likes: number;
//   tags: string[];
//   cardData?: { title?: string; emoji?: string; description?: string };
// }
//
// /** GET /api/plugin/browse */
// export async function searchHFPlugins(): Promise<HFSpace[]> {
//   return apiRequest<HFSpace[]>(`${API_BASE}/api/plugin/browse`);
// }

// Autonomous Agent Skills catalog — proxied through the backend (avoids CORS
// and keeps the catalog host server-side).
// Shapes mirror system/domain/skillstore.go.
export interface StoreSkill {
  id: string;
  name: string;
  slug?: string;
  description?: string;
  version?: string;
  category_id?: string;
  plan_required?: string;
  author?: string;
  license?: string;
  size?: string;
  icon_url?: string;
  compatibility?: string[];
  download_count?: number;
  creator_type?: string;
  source?: string;
}

export interface StoreSkillList {
  data: StoreSkill[];
  total: number;
}

/** One file unpacked from a downloaded `.skill` archive. `text` is inlined for
 *  UTF-8 files; binary or oversized entries carry metadata only. */
export interface SkillBundleFile {
  path: string;
  size: number;
  text?: string;
  binary?: boolean;
  truncated?: boolean;
}

export interface SkillBundle {
  id: string;
  files: SkillBundleFile[];
  skipped?: number;
}

/** GET /api/agent/skills/browse — catalog listing with optional filters. */
export async function browseStoreSkills(
  opts: { keyword?: string; page?: number; limit?: number } = {},
): Promise<StoreSkillList> {
  const q = new URLSearchParams();
  if (opts.keyword) q.set("keyword", opts.keyword);
  if (opts.page) q.set("page", String(opts.page));
  if (opts.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return apiRequest<StoreSkillList>(`${API_BASE}/api/agent/skills/browse${qs ? `?${qs}` : ""}`);
}

/** GET /api/agent/skills/bundle — downloads + unzips the skill server-side and
 *  returns its files. Preview only; nothing is installed. */
export async function fetchSkillBundle(id: string): Promise<SkillBundle> {
  return apiRequest<SkillBundle>(
    `${API_BASE}/api/agent/skills/bundle?id=${encodeURIComponent(id)}`);
}

/** A skill authored in the web UI's "Write skill" form. */
export interface SkillDraft {
  name: string;
  description: string;
  instructions: string;
}

/** POST /api/agent/skills — writes <name>/SKILL.md into the ACTIVE agent
 *  runtime's skills dir. Returns the path written. Rejects with the backend's
 *  message when the runtime can't store authored skills (HTTP 501) or the name
 *  is taken. */
export async function saveSkill(draft: SkillDraft): Promise<{ name: string; path: string }> {
  return apiRequest<{ name: string; path: string }>(`${API_BASE}/api/agent/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
}

/** One node in an installed skill's file tree. `children` is set only on dirs. */
export interface SkillNode {
  name: string;
  path: string;
  dir?: boolean;
  size?: number;
  children?: SkillNode[];
}

/** A skill present in the active runtime's skills dir. */
export interface InstalledSkill {
  name: string;
  description?: string;
  files: SkillNode[];
  /** Newest mtime anywhere in the skill's tree, Unix SECONDS. Omitted when
   *  nothing in the tree could be stat'd. */
  updated_at?: number;
}

/** GET /api/agent/skills — what the ACTIVE runtime currently has installed.
 *  Rejects with the backend's message when the runtime can't list skills
 *  (HTTP 501). An un-provisioned runtime returns an empty list, not an error. */
export async function listInstalledSkills(): Promise<InstalledSkill[]> {
  return apiRequest<InstalledSkill[]>(`${API_BASE}/api/agent/skills`);
}

/** GET /api/agent/skills/files — one installed skill's files with text inlined.
 *  Same `SkillBundle` shape the store preview returns, so both detail views
 *  render through the same component. 404 when the skill is gone (stale list). */
export async function readSkillFiles(name: string): Promise<SkillBundle> {
  return apiRequest<SkillBundle>(
    `${API_BASE}/api/agent/skills/files?name=${encodeURIComponent(name)}`);
}

/** POST /api/agent/skills/upload — installs a `.skill`/`.zip` the operator picked
 *  from their machine. Multipart (not base64) so a multi-MB archive isn't
 *  inflated a third on the wire. */
export async function uploadSkill(file: File): Promise<{ name: string; path: string }> {
  const body = new FormData();
  body.append("file", file);
  // No Content-Type header: the browser must set the multipart boundary itself.
  return apiRequest<{ name: string; path: string }>(
    `${API_BASE}/api/agent/skills/upload`, { method: "POST", body });
}

/** DELETE /api/agent/skills — removes the skill from the ACTIVE runtime's skills
 *  dir. Rejects with the backend's message when it isn't installed (HTTP 404) or
 *  the runtime can't uninstall (HTTP 501). */
export async function deleteSkill(name: string): Promise<{ name: string; path: string }> {
  return apiRequest<{ name: string; path: string }>(
    `${API_BASE}/api/agent/skills?name=${encodeURIComponent(name)}`, { method: "DELETE" });
}

/** POST /api/agent/skills/install — device downloads the catalog's `.skill`
 *  archive and extracts it into the ACTIVE runtime's skills dir. Rejects with
 *  the backend's message when the runtime can't install skills (HTTP 501). */
export async function installStoreSkill(
  id: string, name?: string,
): Promise<{ name: string; path: string }> {
  return apiRequest<{ name: string; path: string }>(`${API_BASE}/api/agent/skills/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, name }),
  });
}

export async function logout(): Promise<boolean> {
  setApiToken("");
  return apiRequest<boolean>(`${API_BASE}/api/logout`, { method: "POST" });
}

// ── Piper: on-device TTS install ──────────────────────────────────────────
//
// Piper ships in no OTA component, so a device that predates it has no
// /opt/piper. The operator installs the engine and downloads voices from
// Settings → Voice; both run as a background job on the device and are
// polled through `job`.

export interface PiperCatalogEntry {
  name: string;
  language: string;
  lang_code: string;
  license: string;
  requires_attribution: boolean;
  size_mb: number;
  installed: boolean;
}
export interface PiperJob {
  active: boolean;
  kind: string;      // "engine" | "voice"
  target: string;
  percent: number;
  error: string;
  done: boolean;
}
export interface PiperStatus {
  engine_installed: boolean;
  voices_installed: string[];
  default_voice: string;
  catalog: PiperCatalogEntry[];
  job: PiperJob;
}

export async function getPiperStatus(): Promise<PiperStatus> {
  return apiRequest<PiperStatus>(`${API_BASE}/api/voice/piper/status`);
}

/** Install the Piper engine. Already-installed returns ok, not an error. */
export async function installPiperEngine(): Promise<{ status: string }> {
  return apiRequest<{ status: string }>(`${API_BASE}/api/voice/piper/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

/** Download one catalogue voice (~63 MB). */
export async function installPiperVoice(name: string): Promise<{ status: string }> {
  return apiRequest<{ status: string }>(`${API_BASE}/api/voice/piper/voice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}
