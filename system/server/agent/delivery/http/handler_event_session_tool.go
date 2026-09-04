package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// handleSessionToolEvent handles WS event=="session.tool": tool events for
// session-subscribed clients (Telegram-initiated turns). Extracted verbatim
// from HandleEvent.
func (h *AgentHandler) handleSessionToolEvent(evt domain.WSEvent) error {
	// Tool events for session-subscribed clients (covers Telegram-initiated turns).
	var payload domain.AgentPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		slog.Warn("session.tool unmarshal error", "component", "agent", "err", err)
		return nil
	}
	// If this tool runs inside a tracked channel turn, map the OpenClaw
	// UUID to the synthetic device runId so tool_call/hw_* flow events
	// share the same run_id as chat_input emitted from session.message.
	if payload.SessionKey != "" && payload.RunID != "" {
		h.channelTurnMu.Lock()
		if st, ok := h.channelTurns[payload.SessionKey]; ok && st.runID != "" {
			h.mapRunID(payload.RunID, st.runID)
		}
		h.channelTurnMu.Unlock()
	}
	flowRunID := h.resolveRunID(payload.RunID)
	toolName := payload.ToolName()
	toolArgs := payload.ToolArguments()
	summary := toolName
	if payload.Data.Phase == "start" {
		summary = fmt.Sprintf("Tool %s started", toolName)
		h.rememberToolArgs(payload.Data.ToolCallID, toolArgs)
		// DEFENSIVE (2026-07-23): rescue [HW:...] markers the agent echoed
		// inside a shell tool call (e.g. `echo '[HW:/audio/play:{...}]'`)
		// instead of emitting them as reply text — a shell echo never reaches
		// the reply-text marker interceptor, so it no-ops (node lights up, HAL
		// gets nothing). Fire them for real; skip the cosmetic-only detection
		// below when we do, to avoid duplicate flow nodes. See fireEchoedHWMarkers.
		echoedHW := h.fireEchoedHWMarkers(toolName, toolArgs, flowRunID)
		// [2026-06-30] Do NOT suppress TTS on /audio/play — the spoken reply
		// must play before music. Python music_service waits for TTS via
		// wait_for_tts() before grabbing ALSA, so the Go-side suppress is
		// redundant and was swallowing the reply. See handler_event_agent.go
		// for the full rationale (mirrors the 2026-05-11 hwCalls fix).
		if !echoedHW && strings.Contains(toolArgs, "/audio/play") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		// Emit specific hardware events for flow monitor visualization.
		// Both flow.Log (for JSONL persistence + UI flow_event triggers) and monitorBus (for SSE).
		if !echoedHW && strings.Contains(toolArgs, "/emotion") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_emotion", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_emotion", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
			if e := parseEmotion(toolArgs); e != "" {
				h.lastEmotionMu.Lock()
				h.lastEmotion = e
				h.lastEmotionMu.Unlock()
			}
		} else if strings.Contains(toolArgs, "/led/solid") ||
			strings.Contains(toolArgs, "/led/effect") ||
			strings.Contains(toolArgs, "/scene") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_led", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		if !echoedHW && strings.Contains(toolArgs, "/led/off") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_led", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		if !echoedHW && (strings.Contains(toolArgs, "/servo/aim") || strings.Contains(toolArgs, "/servo/play")) {
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_servo", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		// Intercept OpenClaw built-in tts tool (session.tool path).
		if toolName == "tts" {
			if ttsText := extractTTSText(toolArgs); ttsText != "" {
				isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
				isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
				isSilent := h.agentGateway.IsSilentRun(flowRunID)
				slog.Info("intercepted built-in tts tool (session.tool), routing to HAL", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat, "silent", isSilent)
				flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
				if !isChannelRun && !isWebChat && !isSilent {
					h.deliverTTS(h.agentGateway.SendToHALTTS, ttsText, flowRunID, "TTS intercept delivery failed")
				}
				h.suppressTTS(payload.RunID, "already_spoken")
			}
		}
	} else if payload.Data.Phase == "end" {
		result := payload.ResultText()
		if len(result) > 100 {
			result = result[:100] + "..."
		}
		summary = fmt.Sprintf("Tool %s done", toolName)
		if result != "" {
			summary += ": " + result
		}
	}
	toolFlowData := map[string]any{"tool": toolName, "phase": payload.Data.Phase, "run_id": flowRunID, "source": "session.tool", "args": toolArgs}
	if snapshotURL := h.snapshotURLForToolCall(payload.Data.ToolCallID, toolArgs, payload.ResultText()); snapshotURL != "" {
		toolFlowData["snapshot_url"] = snapshotURL
	}
	flow.Log("tool_call", toolFlowData, flowRunID)
	toolDetail := map[string]string{"tool": toolName, "args": toolArgs}
	if snapshotURL, ok := toolFlowData["snapshot_url"].(string); ok {
		toolDetail["snapshot_url"] = snapshotURL
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "tool_call",
		Summary: summary,
		RunID:   flowRunID,
		Phase:   payload.Data.Phase,
		Detail:  toolDetail,
	})

	return nil
}
