package mqtthandler

import (
	"context"
	"log/slog"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
)

// whatsappPairTimeout caps the re-pair call. Same shape as add_channel for
// whatsapp but without the bootstrap/restart step before pairing.
const whatsappPairTimeout = 120 * time.Second

func (h *DeviceMQTTHandler) publishWhatsappPairResult(status, errMsg string, evt *domain.PairingEvent) error {
	resp := domain.MQTTWhatsappPairResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, domain.CommandWhatsappPair, device.GetDeviceMac()),
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

func (h *DeviceMQTTHandler) handleWhatsappPair(_ domain.MQTTMessage) error {
	ctx, cancel := context.WithTimeout(context.Background(), whatsappPairTimeout)
	defer cancel()

	events := h.deviceService.PairWhatsapp(ctx)
	for evt := range events {
		status := string(evt.Status)
		if err := h.publishWhatsappPairResult(status, evt.Error, &evt); err != nil {
			slog.Error("whatsapp_pair: publish event failed", "component", "mqtt", "status", status, "error", err)
		}
	}
	slog.Info("whatsapp_pair: stream closed", "component", "mqtt")
	return nil
}
