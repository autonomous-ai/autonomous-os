package http

import "go.autonomous.ai/os/system/domain"

// DeliverHarnessPreparationProgress displays a polled Store snapshot while its
// original main-agent turn is active. It never takes response ownership or
// speaks: main-agent guidance remains available even when preparation fails.
func (h *AgentHandler) DeliverHarnessPreparationProgress(runID, text string) bool {
	if runID == "" || text == "" {
		return false
	}
	h.agentLifecycleMu.Lock()
	runs := make([]string, 0, len(h.activeRunIDBySession))
	for _, id := range h.activeRunIDBySession {
		runs = append(runs, id)
	}
	h.agentLifecycleMu.Unlock()
	active := false
	for _, id := range runs {
		if h.resolveRunID(id) == runID {
			active = true
			break
		}
	}
	if !active {
		return false
	}
	// Serialize against remote final delivery and refuse progress once a task
	// owns this response, so a late preparation poll cannot replace its result.
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	if _, ownsReply := h.harnessReplies[runID]; ownsReply {
		return false
	}
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "assistant_delta", Summary: text, RunID: runID,
			Detail: map[string]string{"role": "assistant", "source": "harness"},
		})
	}
	return true
}
