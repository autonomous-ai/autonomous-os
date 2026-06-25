package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/go-playground/validator/v10"
	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

// pairingQRFormat is the wire-format identifier for the QR text we ship.
// Each char is two vertical QR pixels: '█'=both '▀'=top '▄'=bottom ' '=neither.
const pairingQRFormat = "unicode_blocks_2x1"

// addChannelTimeout caps the whole add_channel call (incl. streaming pairing).
// Budget: ~90s plugin install + 90s QR-scan window + 5min Baileys post-pair
// sync + slack. Set above the sum of those caps so a slow but successful
// flow can't be cut short.
const addChannelTimeout = 10 * time.Minute

func (h *DeviceMQTTHandler) publishAddChannelResult(channel, status, errMsg string, evt *domain.PairingEvent) error {
	resp := domain.MQTTAddChannelResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "add_channel", device.GetDeviceMac()),
		Channel:          channel,
		Status:           status,
		Error:            errMsg,
	}
	if evt != nil && evt.Status == domain.PairingStatusQR {
		resp.PairingQRText = evt.QRText
		resp.PairingQRFormat = pairingQRFormat
		resp.PairingQRSeq = evt.QRSeq
		if !evt.ExpiresAt.IsZero() {
			resp.PairingExpiresAt = evt.ExpiresAt.UTC().Format(time.RFC3339)
		}
	}
	return h.publish(resp)
}

func (h *DeviceMQTTHandler) handleAddChannel(cmd domain.MQTTMessage) error {
	var req domain.MQTTAddChannelCommand
	if err := json.Unmarshal(cmd.Raw(), &req); err != nil {
		slog.Error("add_channel: invalid payload", "component", "mqtt", "error", err)
		return h.publishAddChannelResult(req.Channel, "failure", "invalid JSON payload", nil)
	}

	channelReq := req.ToRequest()
	if err := validator.New().Struct(channelReq); err != nil {
		return h.publishAddChannelResult(req.Channel, "failure", err.Error(), nil)
	}
	if err := channelReq.ValidateChannel(); err != nil {
		return h.publishAddChannelResult(req.Channel, "failure", err.Error(), nil)
	}

	ctx, cancel := context.WithTimeout(context.Background(), addChannelTimeout)
	defer cancel()

	events, err := h.deviceService.AddChannel(ctx, channelReq)
	if err != nil {
		// Map the shared sentinel to a stable code so the backend can branch
		// without parsing free-form text (mirrors channel.refresh_config).
		errMsg := err.Error()
		if errors.Is(err, device.ErrChannelNotSupported) {
			errMsg = "channel_not_supported"
		}
		slog.Error("add_channel: failed", "component", "mqtt", "channel", req.Channel, "code", errMsg, "error", err)
		return h.publishAddChannelResult(req.Channel, "failure", errMsg, nil)
	}

	if events == nil {
		slog.Info("add_channel: success", "component", "mqtt", "channel", req.Channel)
		return h.publishAddChannelResult(req.Channel, "success", "", nil)
	}

	// WhatsApp streams pairing events. Publish one fd_channel message per event.
	for evt := range events {
		status := string(evt.Status)
		if pubErr := h.publishAddChannelResult(req.Channel, status, evt.Error, &evt); pubErr != nil {
			slog.Error("add_channel: publish event failed", "component", "mqtt", "status", status, "error", pubErr)
			// Keep draining so the goroutine in PairWhatsapp can exit cleanly.
		}
	}
	slog.Info("add_channel: pairing stream closed", "component", "mqtt", "channel", req.Channel)
	return nil
}
