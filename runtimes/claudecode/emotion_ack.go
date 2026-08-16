package claudecode

import (
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/skills"
)

// emotion-acknowledge parity for Claude Code.
//
// OpenClaw runs an `emotion-acknowledge` hook (runtimes/openclaw/hooks/emotion-acknowledge/
// handler.ts): on every preprocessed turn it POSTs {emotion:"thinking"} to HAL
// so the body shows it is working before the reply lands. Claude Code executes
// its own native hooks, not the OpenClaw TS handlers, so we reproduce the same
// behavior natively here, fired from sendChat — the same approach as
// codex/emotion_ack.go, hermes/emotion_ack.go and picoclaw/emotion_ack.go.
// Keep this in lockstep with the TS handler — same skip rules, same
// emotion/intensity, same capability gate.
//
// The capability gate is resolved through the SAME registry OpenClaw uses to
// decide whether to install the hook at all (skills.SupportedHooks →
// HookCapability "emotion-acknowledge" = expression). Going through that table
// rather than a hand-rolled cap check keeps the backends consistent if the
// gate ever changes. The companion `turn-gate` hook is intentionally NOT mirrored:
// sendChat already marks the turn busy (busySince/activeTurn) before the network
// round-trip, so a separate gate would be redundant.
const (
	ackEmotionName      = "thinking"
	ackEmotionIntensity = 0.7
)

// ackSkipPrefixes mirror runtimes/openclaw/hooks/emotion-acknowledge/handler.ts exactly: passive
// sensing turns frequently resolve to NO_REPLY, which would leave the face stuck
// on "thinking" with nothing to overwrite it. These are NARROWER than
// deviceInternalPrefixes on purpose — [ambient]/[wellbeing]/wake greetings DO
// produce a spoken reply, so "thinking" is appropriate for them.
var ackSkipPrefixes = []string{
	"[sensing:",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
}

// ackEmotionEnabled reports whether this device installs the emotion-acknowledge
// hook (capability-gated identically to OpenClaw onboarding). Computed once at
// service construction — ROBOT.md does not change at runtime. Fail-open on a
// device that declares no capabilities, matching SupportedHooks.
func ackEmotionEnabled(deviceType string) bool {
	for _, h := range skills.SupportedHooks(device.Capabilities(deviceType)) {
		if h == "emotion-acknowledge" {
			return true
		}
	}
	return false
}

// fireAckEmotion drives the "thinking" face for a turn that will produce a
// visible reply. Mirrors the OpenClaw emotion-acknowledge hook. Fire-and-forget
// (the TS handler ignores POST errors too) and off the caller's goroutine so
// sendChat never blocks on the HAL round-trip.
func (s *ClaudeCodeService) fireAckEmotion(runID, message string) {
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
	// Realtime voice agent already spoke this turn (voice_agent_handled); the run
	// is marked silent (MarkSilentRun) before sendChat runs. Firing "thinking" now
	// would land a few hundred ms after the spoken reply with nothing to overwrite
	// it. This is the Go-native equivalent of the TS hook's `[HANDLED]` text skip —
	// os-server signals "already handled" via the silent-run set, not a body marker.
	if runID != "" && s.IsSilentRun(runID) {
		return
	}
	safego.Go("claudecode-ack-emotion", func() {
		if err := hal.SetEmotion(ackEmotionName, ackEmotionIntensity); err != nil {
			slog.Debug("ack emotion post failed", "component", "claudecode", "error", err)
		}
	})
}
