package mqtthandler

import (
	"encoding/json"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

// scheduleRunPayload is the Data payload for kind:"schedule.run" — the
// "Run now" button. Just an id: cadence/timing are untouched by this kind, so
// there is nothing else to carry.
type scheduleRunPayload struct {
	ID string `json:"id"`
}

// handleScheduleRun handles kind="schedule.run" — fires ONE stored schedule
// immediately through the same Runner the ticker uses, so a manual run and a
// scheduled one report identically over fd_channel. Does not change the
// schedule's cadence or its next_run_at (schedule.Store.SetLastRun, which
// Runner.RunNow uses, is deliberately narrower than the ticker's
// RecordRunResult for exactly this reason).
func (h *DeviceMQTTHandler) handleScheduleRun(env domain.MQTTDataCommand) error {
	var req scheduleRunPayload
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid schedule.run data: "+err.Error(), nil)
	}

	sch, errMsg := resolveScheduleRun(h.scheduleStore, req.ID)
	if errMsg != "" {
		return h.publishDataResult(env.Kind, "failure", errMsg, nil)
	}

	if _, ran := h.scheduleRunner.RunNow(sch); !ran {
		// Deferred, not failed: the agent is mid-turn (single-flight — see
		// Runner.RunNow). RunNow never invoked SendSystemChatMessage in this
		// branch, so publishScheduleRunReport was never called; this is the one
		// place that must still ack the request.
		return h.publishDataResult(env.Kind, "failure", "agent busy, try again shortly",
			map[string]interface{}{"id": sch.ID})
	}
	// RunNow's own report callback (publishScheduleRunReport) already
	// published the terminal ack — nothing left to publish here.
	return nil
}

// resolveScheduleRun looks up id in the store, returning a non-empty message
// when the request can't be run at all (blank id, unknown id). Kept separate
// from the handler so this lookup — including the "unknown id" failure path —
// is testable without a broker.
func resolveScheduleRun(store *schedule.Store, id string) (schedule.Schedule, string) {
	id = strings.TrimSpace(id)
	if id == "" {
		return schedule.Schedule{}, "id is required"
	}
	sch, ok := store.Get(id)
	if !ok {
		return schedule.Schedule{}, "unknown schedule id: " + id
	}
	return sch, ""
}
