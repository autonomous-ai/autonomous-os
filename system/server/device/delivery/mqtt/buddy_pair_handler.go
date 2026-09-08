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
