package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// handleSessionMessageEvent handles WS event=="session.message": channel
// (Telegram) turns that OpenClaw 5.x gates off the agent lifecycle stream.
// Extracted from HandleEvent; outer-switch `break`s became `return nil`.
func (h *AgentHandler) handleSessionMessageEvent(evt domain.WSEvent) error {
	// OpenClaw 5.x gates the `agent` lifecycle stream behind
	// isControlUiVisible (server-chat.ts), so non-device-originated runs
	// (Telegram, etc.) emit no lifecycle_start/end on the agent path.
	// Drive chat_input + HW marker firing for those turns from
	// `session.message` instead. The device's own chat.send flows still use
	// the agent path above — guarded by sessionKey + origin.provider.
	var sm struct {
		SessionKey string `json:"sessionKey"`
		SessionID  string `json:"sessionId"`
		MessageID  string `json:"messageId"`
		MessageSeq int    `json:"messageSeq"`
		Message    struct {
			Role       string          `json:"role"`
			Content    json.RawMessage `json:"content"`
			StopReason string          `json:"stopReason"`
			Timestamp  int64           `json:"timestamp"`
		} `json:"message"`
		Session struct {
			DisplayName string `json:"displayName"`
			Origin      struct {
				Provider string `json:"provider"`
				Surface  string `json:"surface"`
				Label    string `json:"label"`
				From     string `json:"from"`
			} `json:"origin"`
			DeliveryContext struct {
				Channel string `json:"channel"`
			} `json:"deliveryContext"`
		} `json:"session"`
	}
	if err := json.Unmarshal(evt.Payload, &sm); err != nil {
		slog.Warn("session.message unmarshal error", "component", "agent", "err", err)
		return nil
	}
	// Skip heartbeat / cron / proactive turns up front — they may share
	// a telegram session key but must keep the lifecycle path so their
	// reply reaches the device speaker, not just Telegram.
	if sm.Session.Origin.Provider == "heartbeat" {
		return nil
	}
	// Detect inbound channel turns. sessionKey prefix is the most stable
	// signal across OpenClaw versions; origin.provider/deliveryContext
	// are best-effort (sessionRow.origin can be undefined when telegram
	// routes through the default agent session).
	isTelegramChannel := strings.HasPrefix(sm.SessionKey, "agent:main:telegram:") ||
		sm.Session.Origin.Provider == "telegram" ||
		sm.Session.DeliveryContext.Channel == "telegram"
	if !isTelegramChannel {
		return nil
	}
	// Skip if the agent path is already handling this session — cron
	// heartbeat ("Continue the OpenClaw runtime event.") fires both
	// event=agent lifecycle AND session.message; without this guard
	// every heartbeat would emit a duplicate chat_input. Real user
	// telegram does NOT fire event=agent (isControlUiVisible gate),
	// so this map stays empty for them and the handler proceeds.
	const agentLifecycleWindowMs int64 = 30_000
	h.agentLifecycleMu.Lock()
	recentLifecycleMs := h.agentLifecycleAt[sm.SessionKey]
	activeRunID := h.activeRunIDBySession[sm.SessionKey]
	h.agentLifecycleMu.Unlock()
	if recentLifecycleMs > 0 && time.Now().UnixMilli()-recentLifecycleMs < agentLifecycleWindowMs {
		// Queue-mode interleave: a Telegram user message can arrive WHILE a
		// device-issued run (sensing/voice chat.send) is being processed.
		// OpenClaw injects it into the running turn and the agent's reply
		// goes back on the device run's stream — its runID is "device-chat-*"
		// so isDeviceOutboundChatRunID() is true → isChannelRun=false →
		// reply ends up on TTS instead of Telegram. Capture the chat_id
		// here (before the skip) and mark the active run so lifecycle.end
		// suppresses TTS and routes the reply via DM.
		//
		// chat_id sources, in priority order:
		//   1. Conversation-info metadata block in content (when present)
		//   2. sm.Session.DisplayName / Origin.Label regex — these session
		//      fields are populated by OpenClaw for every Telegram broadcast
		//      and don't depend on whether the metadata block was injected.
		isTelegramChannel := strings.HasPrefix(sm.SessionKey, "agent:main:telegram:") ||
			sm.Session.Origin.Provider == "telegram" ||
			sm.Session.DeliveryContext.Channel == "telegram"
		if sm.Message.Role == "user" && activeRunID != "" && isTelegramChannel {
			// Skip the device's own outbound echoes. Origin.Provider on a shared
			// `agent:main:main` session goes "sticky telegram" after any
			// real Telegram turn, so subsequent device-issued chat.send
			// echoes (sensing/voice/wakeup) would otherwise look like
			// Telegram messages and falsely DM the last seen chat_id.
			// Two-layer check: prefix match (deterministic, survives the
			// 30s/32-entry buffer overflow) + IsRecentOutboundChat (catches
			// custom message texts not in the prefix list).
			msgText := extractMessageContentText(sm.Message.Content)
			if msgText != "" && (isDeviceInternalMessage(msgText) || h.agentGateway.IsRecentOutboundChat(msgText)) {
				// fall through to skip log — not a real interleave
			} else {
				chatID := extractTelegramChatID(msgText)
				if chatID == "" {
					chatID = extractTelegramIDFromSenderLabel(sm.Session.DisplayName)
				}
				if chatID == "" {
					chatID = extractTelegramIDFromSenderLabel(sm.Session.Origin.Label)
				}
				if chatID != "" {
					h.channelRunsMu.Lock()
					h.channelRuns[activeRunID] = true
					h.interleavedDMByRunID[activeRunID] = chatID
					h.channelRunsMu.Unlock()
					slog.Info("interleaved Telegram message captured — TTS will be suppressed, reply will DM",
						"component", "agent", "sessionKey", sm.SessionKey,
						"active_run_id", activeRunID, "chat_id", chatID)
				} else {
					slog.Warn("interleaved Telegram detected but chat_id not extractable",
						"component", "agent", "sessionKey", sm.SessionKey,
						"display_name", sm.Session.DisplayName,
						"origin_label", sm.Session.Origin.Label)
				}
			}
		}
		slog.Info("session.message skipped — agent lifecycle active",
			"component", "agent", "sessionKey", sm.SessionKey,
			"ageMs", time.Now().UnixMilli()-recentLifecycleMs)
		return nil
	}
	// Skip echoes of the device's own chat.send messages. session.message
	// arrives BEFORE the corresponding agent lifecycle.start (race), so
	// the lifecycle window above doesn't catch the first turn frame.
	// Match by exact text the device pushed via markOutboundChat (in sendChat),
	// plus a deterministic prefix check so burst voice/sensing turns that
	// overflow the 32-entry recent-outbound buffer or arrive >30s late
	// still get correctly classified as device-internal (not Telegram).
	if sm.Message.Role == "user" {
		text := extractMessageContentText(sm.Message.Content)
		if text != "" && (isDeviceInternalMessage(text) || h.agentGateway.IsRecentOutboundChat(text)) {
			slog.Info("session.message skipped — device-outbound echo",
				"component", "agent", "sessionKey", sm.SessionKey,
				"preview", text[:min(len(text), 80)])
			return nil
		}
	}
	text := extractMessageContentText(sm.Message.Content)

	if sm.Message.Role == "user" {
		runID := "tg-" + sm.MessageID
		if runID == "tg-" {
			runID = fmt.Sprintf("tg-%s-%d", sm.SessionID, sm.MessageSeq)
		}
		senderLabel := sm.Session.DisplayName
		if senderLabel == "" {
			senderLabel = sm.Session.Origin.Label
		}
		// Capture Telegram user ID for outbound DM at lifecycle.end.
		// OpenClaw 5.4 queue mode does NOT auto-deliver replies to the
		// originating Telegram chat when the session is `agent:main:main`
		// (per-sender mode), so the device must DM via Bot API itself. Two
		// signals tried in order: conversation metadata block injected
		// into content (most reliable when present), then senderLabel
		// regex (always available since OpenClaw populates session info).
		telegramID := extractTelegramChatID(text)
		if telegramID == "" {
			telegramID = extractTelegramIDFromSenderLabel(senderLabel)
		}
		h.channelTurnMu.Lock()
		h.channelTurns[sm.SessionKey] = &channelTurnState{
			runID:       runID,
			senderLabel: senderLabel,
			telegramID:  telegramID,
			startedAtMs: sm.Message.Timestamp,
		}
		h.channelTurnMu.Unlock()
		h.channelRunsMu.Lock()
		h.channelRuns[runID] = true
		h.channelRunsMu.Unlock()

		chName := h.agentGateway.GetConfiguredChannel()
		prefix := "[" + chName + "]"
		if senderLabel != "" {
			prefix = "[" + chName + ":" + senderLabel + "]"
		}
		displayMsg := text
		if len(displayMsg) > 200 {
			displayMsg = displayMsg[:200] + "…"
		}
		slog.Info("INBOUND from channel → agent",
			"component", "agent",
			"backend", h.agentGateway.Name(),
			"source", "channel",
			"channel", chName,
			"session_key", sm.SessionKey,
			"run_id", runID,
			"sender", senderLabel,
			"telegram_id", telegramID,
			"msg_len", len(text),
			"message", displayMsg)
		flow.Log("chat_input", map[string]any{
			"run_id":  runID,
			"source":  "channel",
			"message": text,
			"sender":  senderLabel,
		}, runID)
		// Synthesise lifecycle_start so the AGENT pipeline node lights up
		// in Flow Monitor — same anchor the existing agent path emits.
		flow.Log("lifecycle_start", map[string]any{
			"run_id": runID,
			"source": "session.message",
		}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_input",
			Summary: prefix + " " + displayMsg,
			RunID:   runID,
			Detail:  map[string]string{"role": "user", "message": text, "sender": senderLabel},
		})
		return nil
	}

	if sm.Message.Role != "assistant" {
		return nil
	}
	isFinalAssistant := sm.Message.StopReason == "stop" || sm.Message.StopReason == "end_turn"
	h.channelTurnMu.Lock()
	st, ok := h.channelTurns[sm.SessionKey]
	if !ok {
		h.channelTurnMu.Unlock()
		// No tracked turn (user role was skipped earlier — e.g. dedup
		// false-positive). Still clear busy on assistant stop so the
		// turn-gate hook's SetBusy(true) doesn't wedge sensing for 5
		// min. Channel turns are the only path that needs this safety;
		// missing the chat_input/lifecycle synthesis is acceptable
		// (turn just won't show in Flow Monitor for this case).
		if isFinalAssistant {
			slog.Info("session.message untracked assistant stop — clearing busy",
				"component", "agent", "sessionKey", sm.SessionKey)
			h.agentGateway.SetBusy(false)
		}
		return nil
	}
	if text != "" {
		st.accumulated.WriteString(text)
	}
	// stopReason "stop" or "end_turn" both signal the final assistant
	// message of the turn. "toolUse" means another tool round will follow.
	isFinal := sm.Message.StopReason == "stop" || sm.Message.StopReason == "end_turn"
	runID := st.runID
	telegramID := st.telegramID
	var fullText string
	if isFinal {
		fullText = st.accumulated.String()
		delete(h.channelTurns, sm.SessionKey)
	}
	h.channelTurnMu.Unlock()
	if !isFinal {
		return nil
	}

	fullText = prunedImageMarkerRe.ReplaceAllString(fullText, "")
	hwCalls, cleanText := extractHWCalls(fullText)
	cleanText = extractSayTag(cleanText)
	cleanText = sanitizeAgentText(cleanText)

	// ADDED 2026-05-26: drain any leftover firedHWCount for this runID so
	// the per-runID map doesn't leak. Channel turns don't stream-fire
	// markers today (no tryFirstSentenceFlush on this path), so the
	// count is normally 0 — call is defensive against future changes.
	_ = h.consumeFiredHWCount(runID)

	// Fire HW markers (LED, emotion, servo, audio) on the local device
	// even though the spoken text goes back to the originating channel.
	h.fireHWCalls(hwCalls, runID)

	// Synthesise lifecycle_end so RESP node lights up.
	flow.Log("lifecycle_end", map[string]any{
		"run_id": runID,
		"source": "session.message",
	}, runID)
	// Clear the agent busy flag — hal's turn-gate hook called
	// /api/openclaw/busy when this channel turn was preprocessed, but
	// the agent-path lifecycle.end that normally clears it never fires
	// for channel turns (OpenClaw 5.x gate). Without this, sensing
	// events queue for up to busyTTL (5 min) before auto-clearing.
	h.agentGateway.SetBusy(false)

	// Channel turns: TTS stays silent on the speaker. OpenClaw 5.4 queue
	// mode does NOT auto-deliver replies to the originating Telegram chat
	// when session is `agent:main:main`, so the device DMs the reply via Bot API
	// using telegramID captured at channel-turn start.
	switch {
	case isAgentNoReply(cleanText):
		slog.Info("channel turn replied NO_REPLY", "component", "agent", "run_id", runID)
		flow.Log("no_reply", map[string]any{"run_id": runID}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: "[no reply]",
			RunID:   runID,
			State:   "final",
			Detail:  map[string]string{"role": "assistant", "message": "[no reply]"},
		})
	case strings.TrimSpace(cleanText) == "":
		slog.Info("channel turn HW-only reply", "component", "agent", "run_id", runID, "hw_calls", len(hwCalls))
		flow.Log("hw_only_reply", map[string]any{"run_id": runID}, runID)
	default:
		preview := cleanText
		if len(preview) > 200 {
			preview = preview[:200] + "…"
		}
		slog.Info("channel turn final assistant text", "component", "agent",
			"run_id", runID, "hw_calls", len(hwCalls), "telegram_id", telegramID, "text", preview)
		flow.Log("tts_suppressed", map[string]any{
			"run_id": runID,
			"reason": "channel_run",
			"text":   cleanText,
		}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: preview,
			RunID:   runID,
			State:   "final",
			Detail:  map[string]string{"role": "assistant", "message": cleanText},
		})
		// 2026-06-02: Disabled band-aid Telegram DM send below. Users were
		// reporting 2 reply messages per DM turn — OpenClaw upstream now
		// fans out the reply itself, so this code path was the duplicate.
		// REVERT this comment-out (uncomment the block) only if users start
		// reporting "turn ended without visible final response" again, i.e.
		// OpenClaw stops delivering the eventual reply to the originating
		// Telegram chat. The interleave fix elsewhere is a separate case
		// and stays regardless.
		// if telegramID != "" {
		// 	// FIXME: band-aid for OpenClaw 5.4 queue-mode regression. The
		// 	// telegram plugin closes its message-processing window in
		// 	// ~1–2s and reports "turn ended without visible final
		// 	// response" before the agent (which can take 20s+) finishes.
		// 	// The eventual reply lands in chat history but never gets
		// 	// fanned out to the originating Telegram chat. Until OpenClaw
		// 	// fixes that path, the device DMs via Bot API itself. REMOVE this
		// 	// goroutine + flow.Log when upstream fix lands — otherwise
		// 	// users will receive duplicate replies (one from OpenClaw,
		// 	// one from the device). The interleave fix above is a separate
		// 	// case and should stay even after upstream fixes this one.
		// 	go func(t, tid string) {
		// 		slog.Info("channel turn → Telegram DM", "component", "agent", "run_id", runID, "telegram_id", tid)
		// 		if err := h.agentGateway.SendToUser(tid, t, ""); err != nil {
		// 			slog.Error("channel turn DM failed", "component", "agent", "run_id", runID, "err", err)
		// 		}
		// 	}(cleanText, telegramID)
		// 	flow.Log("telegram_dm_send", map[string]any{
		// 		"run_id":      runID,
		// 		"telegram_id": telegramID,
		// 		"source":      "channel_turn",
		// 	}, runID)
		// } else {
		// 	slog.Warn("channel turn has no telegram_id — reply not delivered",
		// 		"component", "agent", "run_id", runID, "sender_label", "elided")
		// }
	}
	// Drop the channelRuns marker — turn is finished.
	h.channelRunsMu.Lock()
	delete(h.channelRuns, runID)
	h.channelRunsMu.Unlock()

	return nil
}
