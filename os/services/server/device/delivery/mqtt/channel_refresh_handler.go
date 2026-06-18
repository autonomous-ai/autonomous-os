package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// channelRefreshTimeout caps the whole channel.refresh_config call. The config
// write is instant; the slow part is `systemctl restart openclaw` waiting for
// the gateway to bind its socket again (~30-60s on a healthy device). Bound it
// so a stuck restart can't deadlock the refresh slot indefinitely.
const channelRefreshTimeout = 5 * time.Minute

// handleChannelRefreshConfig handles kind="channel.refresh_config": re-applies
// the canonical channels.<channel> block on an already-onboarded device using
// the current applySlackChannelConfig writer. Credentials are read from
// config.json on the device — they are NOT carried in the payload.
//
// Async: acks "configuring" (not "starting" — the channel was already set up;
// this is a re-apply), then runs the write+restart in a background goroutine
// (gateway restart can take 30-60s; blocking the broker callback would
// back-pressure the topic). Terminal status published from the goroutine.
func (h *DeviceMQTTHandler) handleChannelRefreshConfig(env domain.MQTTDataCommand) error {
	var req domain.MQTTChannelRefreshConfigData
	if len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &req); err != nil {
			slog.Error("channel.refresh_config: invalid payload", "component", "mqtt", "kind", env.Kind, "error", err)
			return h.publishDataResult(env.Kind, "failure", "invalid channel.refresh_config data: "+err.Error(), nil)
		}
	}
	if req.Channel == "" {
		return h.publishDataResult(env.Kind, "failure", "channel is required", nil)
	}

	slog.Info("channel.refresh_config: received", "component", "mqtt", "channel", req.Channel)

	// "configuring" (not "starting") because the channel was already set up
	// previously — this is a re-apply, not a first-time install.
	if err := h.publishDataResult(env.Kind, "configuring", "", nil); err != nil {
		slog.Warn("channel.refresh_config: ack publish failed", "component", "mqtt", "channel", req.Channel, "error", err)
	}

	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), channelRefreshTimeout)
		defer cancel()

		runtimeStr, err := h.deviceService.RefreshChannelConfig(ctx, req.Channel)
		if err != nil {
			// Map sentinel errors to stable codes so the backend can branch
			// without parsing free-form text.
			errCode := err.Error()
			switch {
			case errors.Is(err, device.ErrSlackCredentialsMissing):
				errCode = "slack_credentials_missing"
			case errors.Is(err, device.ErrChannelNotSupported):
				errCode = "channel_not_supported"
			}
			slog.Error("channel.refresh_config: failed", "component", "mqtt", "channel", req.Channel, "code", errCode, "runtime", runtimeStr, "error", err)
			_ = h.publishDataResult(env.Kind, "failure", errCode, domain.MQTTChannelRefreshConfigResultData{
				Channel: req.Channel,
				Runtime: runtimeStr,
			})
			return
		}
		slog.Info("channel.refresh_config: success", "component", "mqtt", "channel", req.Channel, "runtime", runtimeStr)
		_ = h.publishDataResult(env.Kind, "success", "", domain.MQTTChannelRefreshConfigResultData{
			Channel: req.Channel,
			Runtime: runtimeStr,
		})
	}()
	return nil
}
