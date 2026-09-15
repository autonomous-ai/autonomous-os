package mqtthandler

import (
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// handleEnvironmentStatus serves any HAL environment backend, never a sensor
// model. This is a query of the current snapshot, not a streaming subscription.
func (h *DeviceMQTTHandler) handleEnvironmentStatus(env domain.MQTTDataCommand) error {
	if !device.Capabilities(h.config.DeviceTypeOrDefault())[device.CapEnvironment] {
		return h.publishDataResult(env.Kind, "failure", "environment capability not declared", nil)
	}
	snapshot, err := hal.GetEnvironmentStatus()
	if err != nil {
		return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
	}
	return h.publishDataResult(env.Kind, "success", "", snapshot)
}
