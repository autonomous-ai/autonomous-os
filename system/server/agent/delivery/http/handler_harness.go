package http

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

type harnessReplyState struct {
	webChat   bool
	delivered bool
	toolName  string
	toolArgs  string
}

// MarkHarnessResponseRun holds a user turn open for the final recap from its
// paired Harness agent. webChat selects display-only delivery.
func (h *AgentHandler) MarkHarnessResponseRun(runID string, webChat bool) {
	if runID == "" {
		return
	}
	h.harnessRepliesMu.Lock()
	if h.harnessReplies == nil {
		h.harnessReplies = make(map[string]harnessReplyState)
	}
	h.harnessReplies[runID] = harnessReplyState{webChat: webChat}
	h.harnessRepliesMu.Unlock()
}

// suppressHarnessAgentReply prevents the device agent from announcing a send
// receipt or rewriting a Harness result. A delivered state is cleared here,
// after both the recap and the device agent's lifecycle have completed.
func (h *AgentHandler) suppressHarnessAgentReply(runID string) bool {
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	state, ok := h.harnessReplies[runID]
	if state.delivered {
		delete(h.harnessReplies, runID)
	}
	return ok
}

func (h *AgentHandler) clearHarnessResponseRun(runID string) {
	h.harnessRepliesMu.Lock()
	delete(h.harnessReplies, runID)
	h.harnessRepliesMu.Unlock()
}

// DeliverHarnessProgress shows a lifecycle update emitted by Harness while the
// original user turn remains pending. It deliberately never speaks: voice
// users hear the terminal result, while Web Chat replaces this pending text
// with that exact result when turn.summary arrives.
func (h *AgentHandler) DeliverHarnessProgress(runID, text string) bool {
	if runID == "" || text == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	_, pending := h.harnessReplies[runID]
	h.harnessRepliesMu.Unlock()
	if !pending {
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

// DeliverHarnessTool retains a remote tool start for the voice filler that
// stays alive after the device agent has handed work to Harness. Tool events
// can beat the device agent's NO_REPLY lifecycle by a few milliseconds, so
// the tool is stored as well as forwarded to an already-active filler.
func (h *AgentHandler) DeliverHarnessTool(runID, toolName, toolArgs string) bool {
	if runID == "" || toolName == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	if pending && !state.delivered {
		state.toolName = toolName
		state.toolArgs = toolArgs
		h.harnessReplies[runID] = state
	}
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered {
		return false
	}
	if !state.webChat {
		sensinghttp.DefaultFillerManager.OnToolStart(runID, toolArgs, toolName)
	}
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "assistant_delta", Summary: "Harness is " + toolName + ".", RunID: runID,
			Detail: map[string]string{"role": "assistant", "source": "harness"},
		})
	}
	return true
}

// ResumeHarnessVoiceFillers recreates a voice-only filler after the device
// agent returns NO_REPLY. Its normal lifecycle cancellation must not silence a
// long-running Harness task that it has just delegated.
func (h *AgentHandler) ResumeHarnessVoiceFillers(runID string) {
	h.harnessRepliesMu.Lock()
	state, pending := h.harnessReplies[runID]
	h.harnessRepliesMu.Unlock()
	if !pending || state.delivered || state.webChat {
		return
	}
	sensinghttp.DefaultFillerManager.MarkVoiceRun(runID, "")
	sensinghttp.DefaultFillerManager.OnTurnStart(runID)
	if state.toolName != "" {
		sensinghttp.DefaultFillerManager.OnToolStart(runID, state.toolArgs, state.toolName)
	}
}

// DeliverHarnessResponse emits the paired agent's final recap as the response
// to the original device turn. Web chat is display-only; voice uses normal TTS.
func (h *AgentHandler) DeliverHarnessResponse(runID, text string) bool {
	h.harnessRepliesMu.Lock()
	state, ok := h.harnessReplies[runID]
	if !ok {
		// Older Web/MQTT routing objects used the remote Harness run id instead
		// of the local device run id. A pending web turn is unambiguous here.
		for candidateID, candidate := range h.harnessReplies {
			if candidate.webChat && !candidate.delivered {
				runID, state, ok = candidateID, candidate, true
				break
			}
		}
	}
	wasDelivered := state.delivered
	if ok && !wasDelivered {
		state.delivered = true
		h.harnessReplies[runID] = state
	}
	h.harnessRepliesMu.Unlock()
	if !ok || wasDelivered || text == "" {
		return false
	}
	// A final remote answer replaces any generic progress filler immediately.
	sensinghttp.DefaultFillerManager.Cancel(runID)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "chat_response", Summary: text, RunID: runID, State: "final",
			Detail: map[string]string{"role": "assistant", "message": text, "source": "harness"},
		})
	}
	if !state.webChat {
		go func() {
			if err := hal.SpeakReply(text); err != nil {
				slog.Warn("speak Harness result failed", "component", "harness", "error", err)
			}
		}()
	}
	return true
}
