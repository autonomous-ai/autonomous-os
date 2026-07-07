package codex

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/domain"
)

// Codex supports NO inbound channel (no receive loop exists anywhere — see
// TODO(codex-telegram) in channels.go), so the channel API must refuse
// everything, telegram included, instead of faking success.

func TestCodexSupportedChannels(t *testing.T) {
	got := (&CodexService{}).SupportedChannels()
	if len(got) != 0 {
		t.Fatalf("SupportedChannels() = %v, want empty (codex has no inbound channel)", got)
	}
}

func TestCodexAddChannel(t *testing.T) {
	s := &CodexService{}
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelTelegram})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(telegram) err = %v, want ErrChannelNotSupported (no receive loop under codex)", err)
	}
	err = s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(slack) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestCodexRefreshChannelConfig(t *testing.T) {
	s := &CodexService{}
	_, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelTelegram})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(telegram) err = %v, want ErrChannelNotSupported", err)
	}
	_, err = s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelDiscord})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(discord) err = %v, want ErrChannelNotSupported", err)
	}
}
