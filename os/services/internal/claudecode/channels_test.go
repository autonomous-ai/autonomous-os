package claudecode

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/domain"
)

// The apply path (AddChannel/RefreshChannelConfig → syncChannels →
// EnsureOnboarding) touches /root/.claudecode + systemd, so unit tests cover
// only the capability gate; the apply path is exercised on-device.

func TestClaudeCodeSupportedChannels(t *testing.T) {
	got := (&ClaudeCodeService{}).SupportedChannels()
	if len(got) != 2 || got[0] != domain.ChannelTelegram || got[1] != domain.ChannelDiscord {
		t.Fatalf("SupportedChannels() = %v, want [telegram discord]", got)
	}
}

func TestClaudeCodeAddChannelUnsupported(t *testing.T) {
	s := &ClaudeCodeService{}
	// slack: Claude Code has no slack channel plugin ("Claude in Slack" is a
	// separate cloud feature) — must be rejected, not silently no-op'd.
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(slack) err = %v, want ErrChannelNotSupported", err)
	}
	err = s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestClaudeCodeRefreshChannelConfigUnsupported(t *testing.T) {
	s := &ClaudeCodeService{}
	_, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelSlack})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(slack) err = %v, want ErrChannelNotSupported", err)
	}
}
