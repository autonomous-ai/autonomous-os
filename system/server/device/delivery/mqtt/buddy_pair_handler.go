package mqtthandler

import "go.autonomous.ai/os/system/domain"

// handleBuddyPairStart uses the same service instance as HTTP pairing. Access
// is controlled by the existing authenticated MQTT command channel.
func (h *DeviceMQTTHandler) handleBuddyPairStart(env domain.MQTTDataCommand) error {
	if h.buddyService == nil {
		return h.publishDataResult(env.Kind, "failure", "buddy service unavailable", nil)
	}
	code, ttl := h.buddyService.IssuePairingCode()
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"code":       code,
		"expires_in": int(ttl.Seconds()),
	})
}

// handleBuddyPairRevoke shares the HTTP revocation path, including closing the
// active WebSocket and clearing the persisted pairing.
func (h *DeviceMQTTHandler) handleBuddyPairRevoke(env domain.MQTTDataCommand) error {
	if h.buddyService == nil {
		return h.publishDataResult(env.Kind, "failure", "buddy service unavailable", nil)
	}
	if err := h.buddyService.Unpair(); err != nil {
		return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
	}
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"revoked": true,
	})
}
