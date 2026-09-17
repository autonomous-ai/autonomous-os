package harness

import (
	"context"
	"errors"
	"time"
)

// StepFocus requests an app-owned adjacent selection exactly once. The CLI must
// negotiate focus.step and enforce the revision and idempotency key itself.
func (v *VoiceController) StepFocus(ctx context.Context, id, direction string, generation uint64) (VoiceModeState, error) {
	if id == "" || len(id) > 64 || (direction != "next" && direction != "previous") {
		return v.State(), gestureError("invalid_request", "Invalid focus gesture")
	}
	if !v.op.TryLock() {
		return v.State(), gestureError("busy", "A voice operation is already being handled")
	}
	defer v.op.Unlock()
	v.mu.Lock()
	snapshot, intent := v.state, v.modeIntent
	v.mu.Unlock()
	if !snapshot.Enabled || snapshot.Generation != generation {
		return v.State(), gestureError("mode_changed", "Voice mode or app focus changed")
	}
	status := v.transport.Status()
	if !status.Paired {
		return v.State(), gestureError("harness_unpaired", "Device is not paired with Harness")
	}
	if !status.Connected {
		return v.State(), gestureError("harness_offline", "Harness is offline")
	}
	supported := false
	for _, capability := range status.Capabilities {
		if capability == "focus.step" {
			supported = true
		}
	}
	if !supported {
		return v.State(), gestureError("unsupported", "Harness does not support switching app focus")
	}
	if snapshot.FocusRevision == "" {
		return v.State(), gestureError("focus_unavailable", "App focus revision is unavailable")
	}
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	_, err := v.request(ctx, Frame{
		"type": "focus.step", "idempotencyKey": id,
		"direction": direction, "focusRevision": snapshot.FocusRevision,
	})
	if err != nil {
		code := "focus_unavailable"
		var unknown *DeliveryUnknownError
		if errors.As(err, &unknown) {
			code = "delivery_unknown"
		}
		return v.State(), &VoiceGestureError{Code: code, Cause: err}
	}
	// Refresh through the same authority used by polling. It updates focus only;
	// neither a late RPC nor a late refresh can turn voice mode back on.
	if err := v.RefreshFocus(ctx); err != nil {
		return v.State(), &VoiceGestureError{Code: "focus_unavailable", Cause: err}
	}
	v.mu.Lock()
	changed := v.modeIntent != intent || !v.state.Enabled
	v.mu.Unlock()
	if changed {
		return v.State(), gestureError("mode_changed", "Voice mode changed while switching app focus")
	}
	return v.State(), nil
}
