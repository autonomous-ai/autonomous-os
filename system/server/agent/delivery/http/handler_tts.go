package http

import (
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
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

// runFirstSeenTTL bounds how long an unparseable runID stays in the
// first-seen registry. Well past any real turn duration — the entry only has
// to outlive the turn it describes.
const runFirstSeenTTL = 30 * time.Minute

// runCreatedAtMs returns the unix-ms creation time of the turn behind runID.
//
// Device-issued ids end in the millisecond stamp allocated by the runtime's
// NextChatRunID ("device-chat-7-1755600000000"), so the answer is exact and
// needs no bookkeeping — including for turns that were created before this
// process ever heard of them. Channel ids ("tg-<messageID>") carry no stamp;
// for those the first time we were asked to speak for that run is recorded
// and reused as its age.
func (h *AgentHandler) runCreatedAtMs(runID string) int64 {
	if i := strings.LastIndex(runID, "-"); i >= 0 && i < len(runID)-1 {
		// Only accept a plausible unix-ms stamp: 13 digits keeps a numeric
		// message sequence ("tg-<session>-42") from being read as a date in
		// 1970, which would mute every channel turn forever.
		if suffix := runID[i+1:]; len(suffix) == 13 {
			if ms, err := strconv.ParseInt(suffix, 10, 64); err == nil {
				return ms
			}
		}
	}

	now := time.Now().UnixMilli()
	h.runFirstSeenMu.Lock()
	defer h.runFirstSeenMu.Unlock()
	if seen, ok := h.runFirstSeenMs[runID]; ok {
		return seen
	}
	cutoff := now - runFirstSeenTTL.Milliseconds()
	for id, ts := range h.runFirstSeenMs {
		if ts < cutoff {
			delete(h.runFirstSeenMs, id)
		}
	}
	h.runFirstSeenMs[runID] = now
	return now
}

// isSpeechCancelled reports whether this run's speech was cancelled by the
// physical gesture. True only for turns that already existed when the user
// clicked; anything started afterwards speaks normally.
func (h *AgentHandler) isSpeechCancelled(runID string) bool {
	mark := h.speechWatermarkMs.Load()
	if mark == 0 || runID == "" {
		return false
	}
	// Callers disagree on which id they are holding: the TTS path has already
	// been translated to the device run id, while HW dispatch can still carry
	// the raw backend UUID for the SAME turn. Resolving here keeps one verdict
	// per turn — without it a cancelled run had its speech muted while its
	// servo and LED markers fired anyway (device-observed, 19/8).
	return h.runCreatedAtMs(h.resolveRunID(runID)) <= mark
}

// CancelSpeech silences every turn that is in flight right now. Called by the
// physical cancel gesture (single click on button/touchpad). It deliberately
// does NOT abort the turns: aborting is per-backend and, for several runtimes,
// not expressible at all — whereas taking the speaker away from them is one
// gate that works identically on all six. The user-visible promise is exactly
// "it stops talking", not "it stops thinking".
func (h *AgentHandler) CancelSpeech() {
	now := time.Now().UnixMilli()
	h.speechWatermarkMs.Store(now)
	slog.Info("speech cancelled -- in-flight turns muted",
		"component", "agent", "watermark_ms", now)
	// Monitor bus rather than flow.Log: the click belongs to no single run, and
	// flow events with an empty runID inherit whatever trace happens to be
	// active — which would file the gesture under an unrelated turn. The
	// per-reply tts_cancelled events below carry the real run attribution.
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "speech_cancel",
			Summary: "✋ speech cancelled by click — in-flight turns muted",
			Detail:  map[string]any{"watermark_ms": now},
		})
	}
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
	if h.isSpeechCancelled(flowRunID) {
		slog.Info("TTS dropped -- turn cancelled by physical gesture",
			"component", "agent", "run_id", flowRunID, "text", text[:min(len(text), 80)])
		flow.Log("tts_cancelled", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
		return
	}
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
