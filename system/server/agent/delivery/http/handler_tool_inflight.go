package http

// Open tool-call bookkeeping for the realtime-supersede guard. See the
// openToolCalls field on AgentHandler for why this exists.

// noteToolStart records that callID is executing inside flowRunID. An empty
// run id has nowhere to attach and is ignored; an empty callID (a runtime that
// carries none) is tracked under "" so its start/end still pair up.
func (h *AgentHandler) noteToolStart(flowRunID, callID string) {
	if flowRunID == "" {
		return
	}
	h.openToolMu.Lock()
	defer h.openToolMu.Unlock()
	if h.openToolCalls == nil {
		h.openToolCalls = make(map[string]map[string]struct{})
	}
	calls, ok := h.openToolCalls[flowRunID]
	if !ok {
		calls = make(map[string]struct{})
		h.openToolCalls[flowRunID] = calls
	}
	calls[callID] = struct{}{}
}

// noteToolEnd drops callID from flowRunID's open set. Unknown pairs are a
// no-op: an end that precedes its start (reordered SSE) or follows a restart
// must not create an entry.
func (h *AgentHandler) noteToolEnd(flowRunID, callID string) {
	if flowRunID == "" {
		return
	}
	h.openToolMu.Lock()
	defer h.openToolMu.Unlock()
	calls, ok := h.openToolCalls[flowRunID]
	if !ok {
		return
	}
	delete(calls, callID)
	if len(calls) == 0 {
		delete(h.openToolCalls, flowRunID)
	}
}

// clearOpenTools forgets every open tool of flowRunID. Called at the run's
// lifecycle end/error: whatever was still open died with the turn, and a
// dangling entry would exempt every later realtime reply from the supersede
// mark for the life of the process.
func (h *AgentHandler) clearOpenTools(flowRunID string) {
	if flowRunID == "" {
		return
	}
	h.openToolMu.Lock()
	defer h.openToolMu.Unlock()
	delete(h.openToolCalls, flowRunID)
}

// runsWithOpenTool lists the runs currently executing at least one tool.
// Order is unspecified; callers only need "any" and the ids for the log line.
func (h *AgentHandler) runsWithOpenTool() []string {
	h.openToolMu.Lock()
	defer h.openToolMu.Unlock()
	runs := make([]string, 0, len(h.openToolCalls))
	for run := range h.openToolCalls {
		runs = append(runs, run)
	}
	return runs
}
