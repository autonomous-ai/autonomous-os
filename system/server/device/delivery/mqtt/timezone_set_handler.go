package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

// handleTimezoneSet applies a `timezone.set` downlink — set the device's IANA
// timezone. Same flow as realtime.set: ack immediately, apply async (rewrite
// /etc/localtime + /etc/timezone, persist config.json), then ack the outcome.
// Takes effect without a HAL restart (HAL's clock helpers read /etc/timezone
// fresh per call). See domain.TimezoneSetData for the downlink contract.

func (h *DeviceMQTTHandler) publishTimezoneSetAck(status, errMsg string, data *domain.TimezoneSetData) {
	ack := domain.MQTTTimezoneSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindTimezoneSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("timezone.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleTimezoneSet(env domain.MQTTDataCommand) error {
	var req domain.TimezoneSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("timezone.set: invalid payload", "component", "mqtt", "error", err)
		h.publishTimezoneSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("timezone.set: received", "component", "mqtt", "timezone", req.Timezone)

	// Ack immediately so BFF knows the device received the command.
	h.publishTimezoneSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.SetTimezone(req.Timezone); err != nil {
			slog.Error("timezone.set: SetTimezone failed", "component", "mqtt", "error", err)
			h.publishTimezoneSetAck("failure", err.Error(), &req)
			return
		}
		slog.Info("timezone.set: applied", "component", "mqtt", "timezone", req.Timezone)

		// Re-anchor the scheduler to the new zone. The runner resolves every
		// wall-clock cadence against Store.Timezone(), which is otherwise
		// written ONLY by an inbound schedule.sync — so without this the
		// device keeps firing on the old zone until some unrelated edit
		// happens to trigger a sync. Observed live: a task set for 11:00 kept
		// its 11:00 UTC anchor after a move to Asia/Saigon and was due to fire
		// at 18:02 local.
		h.resyncSchedulesForTimezone(req.Timezone)

		h.publishTimezoneSetAck("success", "", &req)
	}()

	return nil
}

// resyncSchedulesForTimezone re-points the schedule store at tz and recomputes
// every next run, then reports the new times upward on the same
// `schedule.sync` result envelope the cloud already understands — so the app's
// "Next run in N hours" corrects itself without waiting for another sync.
//
// Reuses schedule.SyncSchedules with the schedules ALREADY in the store rather
// than recomputing by hand: that is the one code path that knows how to pair a
// timezone with a next-run computation, and duplicating it here is how the two
// would drift.
//
// Best-effort throughout. The timezone itself is already applied and acked by
// the time this runs, so a failure here costs a stale next_run_at until the
// next sync — it must never turn a successful timezone change into a failed one.
func (h *DeviceMQTTHandler) resyncSchedulesForTimezone(timezone string) {
	changed, err := h.scheduleStore.SetTimezone(timezone)
	if err != nil {
		slog.Error("timezone.set: persist schedule timezone failed",
			"component", "mqtt", "timezone", timezone, "error", err)
		return
	}
	if !changed {
		// A repeat of the zone the store already had: nothing to recompute,
		// and re-publishing would be noise on every duplicate downlink.
		return
	}

	existing, err := h.scheduleStore.Load()
	if err != nil {
		slog.Error("timezone.set: load schedules for recompute failed",
			"component", "mqtt", "error", err)
		return
	}
	if len(existing) == 0 {
		return
	}

	applied, nextRunAt, err := schedule.SyncSchedules(
		h.scheduleStore, existing, timezone, h.config.DeviceID, time.Now())
	if err != nil {
		slog.Error("timezone.set: recompute next runs failed",
			"component", "mqtt", "error", err)
		return
	}

	formatted := make(map[string]string, len(nextRunAt))
	for id, t := range nextRunAt {
		formatted[id] = t.Format(time.RFC3339)
	}
	slog.Info("timezone.set: schedules re-anchored",
		"component", "mqtt", "timezone", timezone, "count", applied)

	if err := h.publishDataResult(domain.KindScheduleSync, "success", "", map[string]interface{}{
		"applied":     applied,
		"next_run_at": formatted,
	}); err != nil {
		slog.Warn("timezone.set: publish recomputed next runs failed",
			"component", "mqtt", "error", err)
	}
}
