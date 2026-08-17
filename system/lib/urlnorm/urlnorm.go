package urlnorm

import "strings"

// autonomousAIBasePath is the campaign-api AI base every autonomous backend is
// mounted under. It identifies the URL by PATH rather than by hostname, because
// the same backend is served from several hosts (production
// campaign-api.autonomous.ai, staging campaign-api.staging.autonomousdev.xyz, and
// local dev), and no third-party base URL ends in this exact path.
const autonomousAIBasePath = "/api/v1/ai"

// NormalizeBaseURL ensures base URLs include the /v1 OpenAI-compat
// prefix so all backends (TTS, STT, LLM, DL) receive a ready-to-use URL without
// each caller having to patch it individually. Non-autonomous URLs are left untouched.
//
// Matching on the path is deliberate. This used to require the literal
// production host "campaign-api.autonomous.ai", so a STAGING device configured
// with ".../api/v1/ai" never got its /v1 and silently 404'd on every route that
// hangs off the base — LLM ({base}/chat/completions) and HAL's Gemini Live
// ({base}/ws/gemini) — while /ping kept working by luck, since beclient strips a
// trailing /v1 that was never there. That made the breakage look like a realtime
// bug rather than a config one.
func NormalizeBaseURL(base string) string {
	base = strings.TrimSuffix(strings.TrimSpace(base), "/")
	if strings.HasSuffix(base, autonomousAIBasePath) {
		base += "/v1"
	}
	return base
}
