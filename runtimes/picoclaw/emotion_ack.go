package picoclaw

import (
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/skills"
)

// Compile-time check: *PicoclawService fires the channel-turn "thinking" ack.
var _ domain.ChannelStartEmotioner = (*PicoclawService)(nil)

// emotion-acknowledge parity for PicoClaw — shows a "thinking" face before the
// reply lands. PicoClaw's channel I/O is gateway-owned (turns never reach sendChat
// where OpenClaw/Hermes fire it), so we hang the ack off the observer's agent:start
// via the optional domain.ChannelStartEmotioner interface. Same skip rules + capability
// gate as the other two backends.
const (
	ackEmotionName      = "thinking"
	ackEmotionIntensity = 0.7
)

// ackSkipPrefixes mirror OpenClaw/Hermes: sensing / device-internal turns often
// resolve to NO_REPLY, which would leave the face stuck on "thinking". Real channel
// messages never carry these prefixes — defensive parity, kept identical.
var ackSkipPrefixes = []string{
	"[sensing:",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
}

// ackEmotionEnabled gates on the `expression` capability, same as OpenClaw/Hermes.
// Computed once at construction — ROBOT.md does not change at runtime.
func ackEmotionEnabled(deviceType string) bool {
	for _, h := range skills.SupportedHooks(device.Capabilities(deviceType)) {
		if h == "emotion-acknowledge" {
			return true
		}
	}
	return false
}

// FireChannelStartEmotion drives the "thinking" face on the observer's agent:start
// (see domain.ChannelStartEmotioner). Fire-and-forget so the hook ACK never blocks on the
// HAL round-trip. runID is unused: channel turns are never realtime-voice replays,
// so the silent-run skip on the sendChat path does not apply here.
func (s *PicoclawService) FireChannelStartEmotion(message, _ string) {
	if !s.ackHookEnabled {
		return
	}
	if strings.TrimSpace(message) == "" {
		return
	}
	for _, p := range ackSkipPrefixes {
		if strings.HasPrefix(message, p) {
			return
		}
	}
	safego.Go("picoclaw-ack-emotion", func() {
		if err := hal.SetEmotion(ackEmotionName, ackEmotionIntensity); err != nil {
			slog.Debug("ack emotion post failed", "component", "picoclaw", "error", err)
		}
	})
}
