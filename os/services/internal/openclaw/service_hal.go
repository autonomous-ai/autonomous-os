package openclaw

import (
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
)

// Regex for stripForTTS — precompiled once at package init. Compiling per-call
// costs ~7 MustCompile per TTS message; at wake-word → reply latency this adds
// measurable overhead.
var (
	reEmoji      = regexp.MustCompile(`[\x{1F300}-\x{1F9FF}\x{2600}-\x{27BF}\x{FE00}-\x{FE0F}\x{200D}\x{20E3}\x{E0020}-\x{E007F}]`)
	reMDBold     = regexp.MustCompile(`\*{1,3}([^*]+)\*{1,3}`)
	reMDItalic   = regexp.MustCompile(`_{1,3}([^_]+)_{1,3}`)
	reMDLink     = regexp.MustCompile(`\[([^\]]+)\]\([^)]+\)`)
	reCodeBlock  = regexp.MustCompile("```[\\s\\S]*?```")
	reInlineCode = regexp.MustCompile("`([^`]+)`")
	reWhitespace = regexp.MustCompile(`\s+`)
)

// StartHALVoice starts the voice pipeline on HAL with API keys from
// config. sttKey / ttsKey + sttBaseURL / ttsBaseURL are split out from
// llmKey / llmBaseURL so households with separate STT / TTS accounts can
// configure each independently. Pass empty for any of them to make HAL
// fall back to the LLM equivalent.
func (s *OpenclawService) StartHALVoice(deepgramKey, llmKey, sttKey, ttsKey, llmBaseURL, sttBaseURL, ttsBaseURL, ttsVoice, ttsInstructions, ttsProvider string) error {
	if deepgramKey == "" {
		return nil
	}
	if err := hal.StartVoice(hal.VoiceStartConfig{
		DeepgramKey:     deepgramKey,
		LLMKey:          llmKey,
		STTKey:          sttKey,
		TTSKey:          ttsKey,
		LLMBaseURL:      llmBaseURL,
		STTBaseURL:      sttBaseURL,
		TTSBaseURL:      ttsBaseURL,
		TTSVoice:        ttsVoice,
		TTSInstructions: ttsInstructions,
		TTSProvider:     ttsProvider,
	}); err != nil {
		return err
	}
	slog.Info("HAL voice pipeline started", "component", "openclaw")
	flow.Log("voice_pipeline_start", nil)
	return nil
}

// stripForTTS removes markdown formatting and emoji so TTS reads clean spoken text.
func stripForTTS(text string) string {
	text = reEmoji.ReplaceAllString(text, "")
	text = reMDBold.ReplaceAllString(text, "$1")
	text = reMDItalic.ReplaceAllString(text, "$1")
	text = reMDLink.ReplaceAllString(text, "$1")
	text = reCodeBlock.ReplaceAllString(text, "")
	text = reInlineCode.ReplaceAllString(text, "$1")
	text = reWhitespace.ReplaceAllString(text, " ")
	return strings.TrimSpace(text)
}

// truncRunes returns the first n runes of s (UTF-8 safe, never cuts mid-char).
func truncRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// SetVolume sets speaker volume on HAL (0-100).
func (s *OpenclawService) SetVolume(pct int) error {
	if err := hal.SetVolume(pct); err != nil {
		return err
	}
	slog.Info("speaker volume set", "component", "openclaw", "pct", pct)
	return nil
}

// StopTTS interrupts active TTS playback and music on HAL immediately,
// freeing the speaker so the voice mic can receive new commands.
func (s *OpenclawService) StopTTS() error {
	if err := hal.StopTTS(); err != nil {
		return err
	}
	// Also stop any music playing — speaker is shared, mic is locked while either runs.
	if err := hal.StopAudio(); err != nil {
		slog.Warn("stop audio failed", "component", "openclaw", "error", err)
	}
	slog.Info("speaker stopped (TTS + music)", "component", "openclaw")
	return nil
}

// SendToHALTTS posts response text to HAL for TTS playback.
// Text must already be stripped of HW markers by the caller (SSE handler).
func (s *OpenclawService) SendToHALTTS(text string) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	// SpeakReply (not Speak): this is the agent's actual reply, so feed it back
	// to the realtime voice agent as history. Hardcoded fillers must NOT route
	// through here — they use hal.Speak directly so they never reach realtime.
	if err := hal.SpeakReply(text); err != nil {
		return fmt.Errorf("speak: %w", err)
	}
	slog.Info("TTS sent", "component", "openclaw", "text", truncRunes(text, 80))

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "tts",
		Summary: text,
	})

	return nil
}

// SendToHALTTSQueue posts response text to HAL's /voice/speak-queue
// endpoint. If the speaker is idle the audio plays immediately (same as
// SendToHALTTS); if a previous speak is still in flight Python queues +
// pre-synthesizes this text and chains it onto the same open ALSA stream so
// the user hears the agent's sentence-streamed reply as one continuous
// utterance.
func (s *OpenclawService) SendToHALTTSQueue(text string) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	if err := hal.SpeakQueueReply(text); err != nil {
		return fmt.Errorf("speak-queue: %w", err)
	}
	slog.Info("TTS queued", "component", "openclaw", "text", truncRunes(text, 80))

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "tts",
		Summary: text,
	})

	return nil
}
