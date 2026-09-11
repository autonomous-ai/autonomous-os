// TTS provider identity, in its own module because both TTSSection (a
// component file) and SettingsPanel need it. Keeping a non-component export in
// TTSSection.tsx breaks React Fast Refresh — react-refresh/only-export-components
// flags it, and its own guidance is to move shared functions to a new file.

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
export type ProviderChoice = "autonomous" | "openai" | "elevenlabs" | "piper" | "custom";

// Detect the current choice from persisted (provider, baseUrl). Anchor on the
// URL host — the raw provider is preserved as the vendor sub-select when the
// choice is Autonomous. If nothing matches a preset we call it Custom rather
// than mis-labelling as Autonomous — an operator with a self-hosted URL
// should see "Custom", not "Autonomous", so switching provider doesn't
// silently overwrite their URL with the campaign-api endpoint.
export function detectChoice(baseUrl: string, provider?: string): ProviderChoice {
  // Piper is URL-less, so the URL heuristic below cannot see it. The saved
  // provider is the only evidence it is selected.
  if (provider === "piper") return "piper";
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
