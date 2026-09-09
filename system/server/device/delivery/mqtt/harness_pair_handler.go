package mqtthandler

import (
	"context"
	"go.autonomous.ai/os/system/domain"
)

func (h *DeviceMQTTHandler) handleHarnessPair(env domain.MQTTDataCommand) error {
	if h.harnessService == nil {
		return h.publishDataResult(env.Kind, "failure", "harness service unavailable", nil)
	}
	switch env.Kind {
	case domain.KindHarnessPairStart:
		info, err := h.harnessService.StartPair(context.Background())
		if err != nil {
			return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
		}
		return h.publishDataResult(env.Kind, "success", "", info)
	case domain.KindHarnessStatus:
		return h.publishDataResult(env.Kind, "success", "", h.harnessService.PairStatus())
	case domain.KindHarnessPairCancel:
		if err := h.harnessService.CancelPair(); err != nil {
			return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
		}
		return h.publishDataResult(env.Kind, "success", "", map[string]bool{"cancelled": true})
	case domain.KindHarnessPairRevoke:
		if err := h.harnessService.Unpair(); err != nil {
			return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
		}
		return h.publishDataResult(env.Kind, "success", "", map[string]bool{"unpaired": true})
	}
	return nil
}
