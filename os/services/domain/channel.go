package domain

import "errors"

// ErrChannelNotSupported is returned by AddChannel / RefreshChannelConfig when the
// active runtime cannot run the requested channel. Shared across every runtime, the
// device layer, and the MQTT handlers so callers compare one sentinel value.
var ErrChannelNotSupported = errors.New("channel_not_supported")

// ErrChannelCredentialsMissing is returned when a refresh/add cannot proceed because
// config.json carries no credentials for the channel (e.g. a refresh requested for a
// channel that was never set up).
var ErrChannelCredentialsMissing = errors.New("channel_credentials_missing")

// ChannelSupported reports whether gw lists channel in its SupportedChannels().
func ChannelSupported(gw AgentGateway, channel string) bool {
	for _, c := range gw.SupportedChannels() {
		if c == channel {
			return true
		}
	}
	return false
}
