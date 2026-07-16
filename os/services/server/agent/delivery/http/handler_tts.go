package http

import (
	"errors"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// llmLimitPatterns fingerprint the plan-usage-limit banner the backend returns
// IN PLACE of a real reply when the LLM quota is exhausted ("⚠️ I've reached
// the usage limit for your current plan. You can upgrade for higher limits, or
// your access will reset after N hours... Upgrade your plan → https://...").
// The banner streams sentence-by-sentence, so each chunk is matched on its own
// fragment. Lower-case; match with strings.Contains on the lowered text.
var llmLimitPatterns = []string{
	"reached the usage limit",
	"upgrade for higher limits",
	"your access will reset",
	"upgrade your plan",
}

func isLLMLimitText(text string) bool {
	t := strings.ToLower(text)
	for _, p := range llmLimitPatterns {
		if strings.Contains(t, p) {
			return true
		}
	}
	return false
}

// deliverTTS sends text to HAL in a background goroutine and logs the outcome:
// hal.ErrSpeakerMuted becomes a tts_muted flow event so the Flow Monitor can
// show that the reply was displayed but never spoken; any other error is a
// real delivery failure.
//
// LLM-limit banners never reach the speaker verbatim: reading English
// marketing text plus a URL aloud is useless to the user (and when TTS shares
// the exhausted key it would just 429 anyway). The spoken text is swapped for
// a short localized notice, debounced so the multi-sentence banner (and every
// following turn while the plan stays exhausted) announces at most once per
// window. Web chat is untouched — it renders the full banner with the link.
func (h *AgentHandler) deliverTTS(send func(string) error, text, flowRunID, errCtx string) {
	if isLLMLimitText(text) {
		now := time.Now().UnixMilli()
		last := h.lastLLMLimitTTS.Load()
		if now-last < 30_000 || !h.lastLLMLimitTTS.CompareAndSwap(last, now) {
			slog.Info("LLM limit banner chunk suppressed (already announced)",
				"component", "agent", "run_id", flowRunID)
			return
		}
		slog.Warn("LLM usage-limit reply detected — speaking short notice instead",
			"component", "agent", "run_id", flowRunID, "banner", text[:min(len(text), 120)])
		flow.Log("tts_llm_limit", map[string]any{"run_id": flowRunID, "banner": text}, flowRunID)
		// The notice is OS-generated hardcoded TTS, NOT the agent's reply.
		// SpeakCached: (a) plain path — never fed back into the realtime
		// [TTS HISTORY]; (b) render+save on first success, then replays from
		// hal's persistent WAV cache with no API call — which is what keeps
		// it audible when the TTS provider shares the exhausted quota.
		text = i18n.One(i18n.PhraseLLMLimit)
		send = hal.SpeakCached
	}
	go func() {
		err := send(text)
		if err == nil {
			return
		}
		if errors.Is(err, hal.ErrSpeakerMuted) {
			slog.Info("TTS muted -- speaker muted on device", "component", "agent", "run_id", flowRunID, "text", text[:min(len(text), 80)])
			flow.Log("tts_muted", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
			return
		}
		slog.Error(errCtx, "component", "agent", "error", err)
	}()
}
