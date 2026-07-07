package codex

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/domain"
)

func TestCodexSupportedChannels(t *testing.T) {
	got := (&CodexService{}).SupportedChannels()
	if len(got) != 1 || got[0] != domain.ChannelTelegram {
		t.Fatalf("SupportedChannels() = %v, want [telegram]", got)
	}
}

func TestCodexAddChannel(t *testing.T) {
	s := &CodexService{}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelTelegram}); err != nil {
		t.Errorf("AddChannel(telegram) err = %v, want nil", err)
	}
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(slack) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestCodexRefreshChannelConfig(t *testing.T) {
	s := &CodexService{}
	if _, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelTelegram}); err != nil {
		t.Errorf("RefreshChannelConfig(telegram) err = %v, want nil", err)
	}
	_, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelDiscord})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(discord) err = %v, want ErrChannelNotSupported", err)
	}
}
