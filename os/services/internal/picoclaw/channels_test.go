package picoclaw

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/domain"
)

func TestPicoclawSupportedChannels(t *testing.T) {
	got := (&PicoclawService{}).SupportedChannels()
	if len(got) != 1 || got[0] != domain.ChannelTelegram {
		t.Fatalf("SupportedChannels() = %v, want [telegram]", got)
	}
}

func TestPicoclawAddChannel(t *testing.T) {
	s := &PicoclawService{}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelTelegram}); err != nil {
		t.Errorf("AddChannel(telegram) err = %v, want nil", err)
	}
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(slack) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestPicoclawRefreshChannelConfig(t *testing.T) {
	s := &PicoclawService{}
	if _, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelTelegram}); err != nil {
		t.Errorf("RefreshChannelConfig(telegram) err = %v, want nil", err)
	}
	_, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelDiscord})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(discord) err = %v, want ErrChannelNotSupported", err)
	}
}
