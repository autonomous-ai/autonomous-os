package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
	sensinghttp "go.autonomous.ai/os/server/sensing/delivery/http"
)

// handleAgentStreamEvent handles WS event=="agent": the OpenClaw agent stream
// (lifecycle / tool / thinking / assistant) plus the lifecycle-end TTS flush.
// Extracted verbatim from HandleEvent; dispatch lives in handler_events.go.
func (h *AgentHandler) handleAgentStreamEvent(evt domain.WSEvent) error {
	var payload domain.AgentPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		return err
	}
	// Capture session key from any agent event
	if payload.SessionKey != "" && h.agentGateway.GetSessionKey() == "" {
		h.agentGateway.SetSessionKey(payload.SessionKey)
	}

	// Map OpenClaw UUID → device idempotencyKey on lifecycle_start.
	// Only map when the lifecycle belongs to the device's own direct session — group/channel
	// sessions have independent runs that must NOT be merged into sensing traces.
	//
	// Two paths depending on payload.RunID format:
	//   • device-format (device-chat-*): OpenClaw 5.4+ echoes the idempotencyKey as
	//     the runId — already IS the device trace. Just remove from pending.
	//   • UUID: produced when OpenClaw drains its followup queue (the
	//     FollowupRun type does not carry idempotencyKey, so
	//     agent-runner-execution.ts mints a fresh UUID at lifecycle time).
	//     Resolve by fetching chat.history and matching the agent's last
	//     user message against the stored pending text. Correct by content
	//     rather than by send-order — drain reordering, dropped turns,
	//     /new session clears, and concurrent channel UUIDs no longer
	//     shift the mapping.
	//
	// This runs synchronously: the WS read loop now dispatches handler
	// events through a worker goroutine (service_ws.go), so chat.history's
	// pendingRPC wait no longer deadlocks against the read loop. Sync map
	// before flowRunID is computed below — every subsequent event for this
	// UUID resolves to the device id from the very first emit, eliminating
	// the split-turn race the previous async version had.
	agentSession := h.agentGateway.GetSessionKey()
	isAgentSession := agentSession != "" && payload.SessionKey == agentSession
	if payload.Stream == "lifecycle" && payload.Data.Phase == "start" && payload.RunID != "" && isAgentSession {
		if isDeviceOutboundChatRunID(payload.RunID) {
			h.agentGateway.RemovePendingChatTraceByRunID(payload.RunID)
		} else {
			hist, err := h.agentGateway.FetchChatHistory(payload.SessionKey, 5)
			if err == nil && hist != nil {
				if userMsg, _ := extractLastUserMessageFromHistory(hist); userMsg != "" {
					if deviceTrace := h.agentGateway.MatchPendingByMessage(userMsg); deviceTrace != "" {
						h.mapRunID(payload.RunID, deviceTrace)
						slog.Info("mapped OpenClaw runId to device trace via chat.history",
							"component", "agent", "openclawId", payload.RunID, "deviceId", deviceTrace)
						slog.Info("flow correlation", "op", "openclaw_uuid_map", "section", "openclaw",
							"openclaw_run_id", payload.RunID, "device_run_id", deviceTrace,
							"note", "matched via chat.history last user message text")
					}
				}
			} else if err != nil {
				slog.Warn("chat.history fetch failed at UUID lifecycle_start (skipping map)",
					"component", "agent", "run_id", payload.RunID, "err", err)
			}
		}
	}

	// Resolve OpenClaw UUID → device ID for consistent flow tracing across all agent events
	flowRunID := h.resolveRunID(payload.RunID)
	switch payload.Stream {
	case "lifecycle":
		slog.Info("lifecycle event", "component", "agent", "phase", payload.Data.Phase, "runId", payload.RunID, "flowRunId", flowRunID, "session", payload.SessionKey)

		// Track agent-path activity per sessionKey so the session.message
		// handler can skip turns already driven by the agent stream
		// (cron heartbeat fires both; real user telegram fires only
		// session.message because of OpenClaw's isControlUiVisible gate).
		// Clear on end/error so a subsequent channel turn on the same
		// session within 30s isn't wrongly skipped — the previous turn
		// is finished, agent path is no longer handling anything.
		if payload.SessionKey != "" {
			h.agentLifecycleMu.Lock()
			switch payload.Data.Phase {
			case "start":
				h.agentLifecycleAt[payload.SessionKey] = time.Now().UnixMilli()
				if payload.RunID != "" {
					h.activeRunIDBySession[payload.SessionKey] = payload.RunID
				}
			case "end", "error":
				delete(h.agentLifecycleAt, payload.SessionKey)
				delete(h.activeRunIDBySession, payload.SessionKey)
			}
			h.agentLifecycleMu.Unlock()
		}

		// Correlate with the FIFO queue of recent cron "started" events:
		// the cron event lacks the upcoming runId AND (for sessionTarget=
		// "main" jobs) lacks sessionKey too, so we consume the oldest
		// timestamp within cronFireWindowMs. Restricted to UUID runIds
		// (no device- prefix) so chat.send/sensing turns can't accidentally
		// claim a queued cron slot.
		if payload.Data.Phase == "start" && payload.RunID != "" && !isDeviceOutboundChatRunID(payload.RunID) {
			now := time.Now().UnixMilli()
			cutoff := now - cronFireWindowMs
			h.cronFireExpectedMu.Lock()
			// Drop stale entries from the head.
			idx := 0
			for idx < len(h.cronFireExpected) && h.cronFireExpected[idx] < cutoff {
				idx++
			}
			h.cronFireExpected = h.cronFireExpected[idx:]
			if len(h.cronFireExpected) > 0 {
				startedAt := h.cronFireExpected[0]
				h.cronFireExpected = h.cronFireExpected[1:]
				h.cronFireExpectedMu.Unlock()
				h.cronFireRunsMu.Lock()
				h.cronFireRuns[payload.RunID] = true
				h.cronFireRunsMu.Unlock()
				slog.Info("cron fire correlated — will force TTS", "component", "agent", "run_id", payload.RunID, "session", payload.SessionKey, "delta_ms", now-startedAt)
				// Emit a cron_fire flow event so the web monitor can classify
				// this turn as cron without re-deriving via string match on
				// the systemEvent wrapper template.
				flow.Log("cron_fire", map[string]any{"run_id": payload.RunID, "delta_ms": now - startedAt}, payload.RunID)
			} else {
				h.cronFireExpectedMu.Unlock()
			}
		}

		// Detect external channel-initiated turns: lifecycle_start arrives from OpenClaw
		// with a UUID run_id (not device-chat-* prefix). This covers:
		// 1. No active trace (original case)
		// 2. Active trace from a different turn (sensing trace still active when Telegram arrives)
		//
		// Cron-fire turns also have UUID runIds but are NOT channel input —
		// the cron_fire flow event represents them in the monitor, so skip
		// the chat_input emit here to keep the CH IN node from lighting up
		// for scheduled reminders.
		h.cronFireRunsMu.Lock()
		isCronFireTurn := h.cronFireRuns[payload.RunID]
		h.cronFireRunsMu.Unlock()
		isChannelTurn := payload.Data.Phase == "start" && payload.RunID != "" &&
			!isDeviceOutboundChatRunID(payload.RunID) && !isDeviceOutboundChatRunID(flowRunID) &&
			!isCronFireTurn
		if isChannelTurn {
			// Emit chat_input immediately so UI shows turn-started.
			// Use a neutral "[chat]" placeholder rather than claiming the
			// configured channel — the goroutine below will replace this
			// with the right label ([telegram:Gray] / [voice] / [emotion]
			// / ...) once chat.history reveals whether it's a real
			// channel user or a device-internal sensing/voice merge. If
			// the goroutine fails or times out, this generic label
			// stays — better than mis-attributing to Telegram.
			flow.Log("chat_input", map[string]any{"run_id": payload.RunID, "source": "channel"}, payload.RunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_input",
				Summary: "[chat]",
				RunID:   payload.RunID,
				Detail:  map[string]string{"role": "user"},
			})

			// Best-effort: fetch chat history in a separate goroutine to avoid
			// deadlocking the WS read loop (FetchChatHistory waits for a response
			// that can only arrive after this handler returns).
			capturedRunID := payload.RunID
			capturedSessionKey := payload.SessionKey
			go func() {
				historyPayload, histErr := h.agentGateway.FetchChatHistory(capturedSessionKey, 20)
				if histErr != nil {
					slog.Warn("chat.history fetch failed (best-effort)", "component", "agent", "run_id", capturedRunID, "err", histErr)
					return
				}
				if historyPayload == nil {
					return
				}
				slog.Info("chat.history for channel turn", "component", "agent", "run_id", capturedRunID, "history_bytes", len(historyPayload))
				// Dump the last message raw JSON — helps identify a cleaner cron-fire
				// signal (e.g. role:"system", kind:"systemEvent") than string matching.
				// Temporary — remove once schema is confirmed.
				if len(historyPayload) < 8000 {
					slog.Info("chat.history raw payload", "component", "agent", "run_id", capturedRunID, "payload", string(historyPayload))
				}

				userMsg, senderLabel := extractLastUserMessageFromHistory(historyPayload)
				// Mark as confirmed channel run if a real sender is present.
				// Guards against race: Telegram UUID mapped to sensing trace
				// makes flowRunID = device-sensing-* → isChannelRun wrongly false.
				if senderLabel != "" {
					h.channelRunsMu.Lock()
					h.channelRuns[capturedRunID] = true
					h.channelRunsMu.Unlock()
				}
				// Cron-fire detection happens at lifecycle_start (see correlation
				// against cronFireExpected) — no need to inspect userMsg here.
				if userMsg != "" {
					// Legacy: detect old music-proactive cron turns (before event-driven suggestion).
					// Safe to remove once all devices have been updated and old crons are cleaned up.
					if strings.Contains(userMsg, "[music-proactive]") {
						resolved := h.resolveRunID(capturedRunID)
						h.agentGateway.MarkBroadcastRun(resolved)
					}

					displayMsg := userMsg
					if len(displayMsg) > 200 {
						displayMsg = displayMsg[:200] + "…"
					}
					// Label selection (priority order):
					//  1. Real channel user (senderLabel filled by chat.history) →
					//     `[telegram:Gray]` — keeps existing Telegram UI.
					//  2. device-internal sensing/voice/wellbeing/system message
					//     merged into this UUID turn via OpenClaw steer →
					//     `[voice]` / `[emotion]` / `[activity]` / ... so the
					//     monitor doesn't mis-label self-fire turns as
					//     `[telegram]`.
					//  3. Fallback: generic `[chat]` — UUID with no sender and
					//     no recognisable internal prefix (rare; was previously
					//     mis-labelled as the configured channel).
					chName := h.agentGateway.GetConfiguredChannel()
					var prefix string
					switch {
					case senderLabel != "":
						prefix = "[" + chName + ":" + senderLabel + "]"
					default:
						if lbl := labelForDeviceInternal(userMsg); lbl != "" {
							prefix = lbl
						} else {
							prefix = "[chat]"
						}
					}
					flow.Log("chat_input", map[string]any{
						"run_id":  capturedRunID,
						"source":  "channel",
						"message": userMsg,
						"sender":  senderLabel,
					}, capturedRunID)
					h.monitorBus.Push(domain.MonitorEvent{
						Type:    "chat_input",
						Summary: prefix + " " + displayMsg,
						RunID:   capturedRunID,
						Detail:  map[string]string{"role": "user", "message": userMsg, "sender": senderLabel},
					})
				}
			}()
		}

		// Track busy state so passive sensing events can be suppressed during active turns.
		// Only gate on lifecycles that belong to a device-initiated turn — these are
		// the only ones whose `end` is reliably round-tripped through SSE.
		// Heartbeat (target:"none"), channel turns merged by steer mode, and other
		// OpenClaw self-trigger lifecycles can drop their `end` SSE (per the
		// busyTTL comment in service_events.go); gating on them strands activeTurn=true
		// for up to 5 minutes — every device sensing event in that window queues
		// instead of forwarding.
		//
		// External turns don't NEED device-side gating: with messages.queue.mode=steer
		// (pinned in onboarding), concurrent sensing events arriving during a
		// channel/cron turn are batched into the active turn at the next model
		// boundary by OpenClaw itself — no need for the device to pre-suppress them.
		//
		// device-initiated turns also flip activeTurn=true at chat.send time
		// (service_chat.go), so a missed lifecycle.start here is harmless.
		// LED is managed by the agent via /emotion skill calls — do not override here.
		if payload.Data.Phase == "start" {
			deviceInitiated := isDeviceOutboundChatRunID(payload.RunID) || isDeviceOutboundChatRunID(flowRunID)
			if deviceInitiated {
				h.agentGateway.SetBusy(true)
			} else {
				slog.Info("lifecycle.start skipped for busy gating",
					"component", "agent", "run_id", payload.RunID, "flow_run_id", flowRunID,
					"reason", "not device-initiated — heartbeat/channel/cron handled by OpenClaw steer batching")
			}
			// Arm the dead-air filler timer for voice turns. No-op
			// unless sensing handler called MarkVoiceRun(flowRunID)
			// before forwarding this turn.
			sensinghttp.DefaultFillerManager.OnTurnStart(flowRunID)
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			h.agentGateway.SetBusy(false)
			// Cancel on error too — lifecycle.end has its own Cancel
			// further down (just before TTS flush), but error skips
			// that block, so clean filler state here.
			if payload.Data.Phase == "error" {
				sensinghttp.DefaultFillerManager.Cancel(flowRunID)
				// The lifecycle-end Slack finalize (further down) is skipped on error,
				// so clean up any Slack stream here. DeliverSlackReply("") consumes the
				// origin, stops the per-run stream goroutine, and posts nothing; it's a
				// no-op for non-Slack runs (and runtimes that aren't a SlackBridge).
				if sb, ok := h.agentGateway.(domain.SlackBridge); ok {
					go func() {
						if err := sb.DeliverSlackReply(flowRunID, ""); err != nil {
							slog.Error("slack cleanup on error failed", "component", "agent", "err", err)
						}
					}()
				}
			}
		}

		// Token usage: try lifecycle_end payload first, fallback to chat.history RPC.
		if payload.Data.Phase == "end" {
			slog.Info("lifecycle end raw", "component", "agent", "runId", payload.RunID, "raw", string(evt.Payload))
			if u := payload.Data.Usage; u != nil {
				slog.Info("token usage", "component", "agent", "runId", payload.RunID,
					"input", u.InputTokens, "output", u.OutputTokens,
					"cacheRead", u.CacheReadTokens, "cacheWrite", u.CacheWriteTokens,
					"total", u.TotalTokens)
				flow.Log("token_usage", map[string]any{
					"run_id":             flowRunID,
					"input_tokens":       u.InputTokens,
					"output_tokens":      u.OutputTokens,
					"cache_read_tokens":  u.CacheReadTokens,
					"cache_write_tokens": u.CacheWriteTokens,
					"total_tokens":       u.TotalTokens,
				}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "token_usage",
					Summary: fmt.Sprintf("in:%d out:%d total:%d", u.InputTokens, u.OutputTokens, u.TotalTokens),
					RunID:   flowRunID,
					Detail: map[string]string{
						"input_tokens":       fmt.Sprintf("%d", u.InputTokens),
						"output_tokens":      fmt.Sprintf("%d", u.OutputTokens),
						"cache_read_tokens":  fmt.Sprintf("%d", u.CacheReadTokens),
						"cache_write_tokens": fmt.Sprintf("%d", u.CacheWriteTokens),
						"total_tokens":       fmt.Sprintf("%d", u.TotalTokens),
					},
				})
			} else {
				// OpenClaw lifecycle_end does not include usage. Fetch from chat.history instead.
				capturedFlowRunID := flowRunID
				capturedSessionKey := payload.SessionKey
				go func() {
					histPayload, err := h.agentGateway.FetchChatHistory(capturedSessionKey, 5)
					if err != nil {
						slog.Warn("chat.history usage fetch failed", "component", "agent", "run_id", capturedFlowRunID, "err", err)
						return
					}
					if histPayload == nil {
						return
					}
					type histUsage struct {
						Input       int `json:"input"`
						Output      int `json:"output"`
						TotalTokens int `json:"totalTokens"`
						CacheRead   int `json:"cacheRead"`
						CacheWrite  int `json:"cacheWrite"`
					}
					type histContent struct {
						Type     string `json:"type"`
						Text     string `json:"text,omitempty"`
						Thinking string `json:"thinking,omitempty"`
					}
					var hist struct {
						Messages []struct {
							Role    string        `json:"role"`
							Usage   *histUsage    `json:"usage,omitempty"`
							Content []histContent `json:"content,omitempty"`
						} `json:"messages"`
					}
					if json.Unmarshal(histPayload, &hist) != nil {
						return
					}
					// Extract thinking from last assistant message and emit to monitor
					for i := len(hist.Messages) - 1; i >= 0; i-- {
						if hist.Messages[i].Role == "assistant" {
							for _, c := range hist.Messages[i].Content {
								if c.Type == "thinking" && c.Thinking != "" {
									flow.Log("agent_thinking", map[string]any{
										"run_id": capturedFlowRunID,
										"source": "chat_history",
										"text":   c.Thinking,
									}, capturedFlowRunID)
									h.monitorBus.Push(domain.MonitorEvent{
										Type:    "thinking",
										Summary: c.Thinking,
										RunID:   capturedFlowRunID,
									})
								}
							}
							break
						}
					}
					// Find last assistant message with usage.
					for i := len(hist.Messages) - 1; i >= 0; i-- {
						if hist.Messages[i].Role == "assistant" && hist.Messages[i].Usage != nil {
							u := hist.Messages[i].Usage
							slog.Info("token usage (from chat.history)", "component", "agent",
								"run_id", capturedFlowRunID,
								"input", u.Input, "output", u.Output,
								"cacheRead", u.CacheRead, "cacheWrite", u.CacheWrite,
								"total", u.TotalTokens)
							flow.Log("token_usage", map[string]any{
								"run_id":             capturedFlowRunID,
								"source":             "chat_history",
								"input_tokens":       u.Input,
								"output_tokens":      u.Output,
								"cache_read_tokens":  u.CacheRead,
								"cache_write_tokens": u.CacheWrite,
								"total_tokens":       u.TotalTokens,
							}, capturedFlowRunID)
							h.monitorBus.Push(domain.MonitorEvent{
								Type:    "lifecycle",
								Summary: fmt.Sprintf("Agent end — tokens: %d in / %d out", u.Input, u.Output),
								RunID:   capturedFlowRunID,
								Detail: map[string]string{
									"inputTokens":  fmt.Sprintf("%d", u.Input),
									"outputTokens": fmt.Sprintf("%d", u.Output),
									"cacheRead":    fmt.Sprintf("%d", u.CacheRead),
									"cacheWrite":   fmt.Sprintf("%d", u.CacheWrite),
									"totalTokens":  fmt.Sprintf("%d", u.TotalTokens),
								},
							})
							h.monitorBus.Push(domain.MonitorEvent{
								Type:    "token_usage",
								Summary: fmt.Sprintf("in:%d out:%d total:%d", u.Input, u.Output, u.TotalTokens),
								RunID:   capturedFlowRunID,
								Detail: map[string]string{
									"input_tokens":       fmt.Sprintf("%d", u.Input),
									"output_tokens":      fmt.Sprintf("%d", u.Output),
									"cache_read_tokens":  fmt.Sprintf("%d", u.CacheRead),
									"cache_write_tokens": fmt.Sprintf("%d", u.CacheWrite),
									"total_tokens":       fmt.Sprintf("%d", u.TotalTokens),
								},
							})
							// Auto-compact (legacy) — slow but preserves verbatim history
							// via generated summary. Disabled in favour of new-session
							// below; restore by uncommenting if new-session causes memory
							// regressions. See maybeAutoCompact + maybeAutoNewSession in
							// handler_session_lifecycle.go for trade-offs.
							// h.maybeAutoCompact(h.agentGateway.GetSessionKey(), u.TotalTokens, capturedFlowRunID)

							// Auto-new-session — instant, drops in-session conversation
							// history but keeps device external memory (mood/habit/owner).
							h.maybeAutoNewSession(h.agentGateway.GetSessionKey(), u.TotalTokens, capturedFlowRunID)
							break
						}
					}
				}()
			}
		}

		shortErr := shortError(payload.Data.Error)
		flow.Log("lifecycle_"+payload.Data.Phase, map[string]any{"run_id": flowRunID, "error": payload.Data.Error}, flowRunID)
		monEvt := domain.MonitorEvent{
			Type:    "lifecycle",
			Summary: fmt.Sprintf("Agent %s", payload.Data.Phase),
			RunID:   flowRunID,
			Phase:   payload.Data.Phase,
			Error:   shortErr,
		}
		if payload.Data.Phase == "error" && shortErr != "" {
			monEvt.Summary = "❌ " + shortErr
		}
		if payload.Data.Phase == "end" && payload.Data.Usage != nil {
			u := payload.Data.Usage
			monEvt.Detail = map[string]string{
				"inputTokens":  fmt.Sprintf("%d", u.InputTokens),
				"outputTokens": fmt.Sprintf("%d", u.OutputTokens),
				"cacheRead":    fmt.Sprintf("%d", u.CacheReadTokens),
				"cacheWrite":   fmt.Sprintf("%d", u.CacheWriteTokens),
				"totalTokens":  fmt.Sprintf("%d", u.TotalTokens),
			}
			monEvt.Summary = fmt.Sprintf("Agent end — tokens: %d in / %d out", u.InputTokens, u.OutputTokens)
		}
		h.monitorBus.Push(monEvt)

		// Keep flow.GetTrace() "active" for the duration of the device turn so Telegram heuristic
		// (lifecycle_start arriving while no device trace is active) can work correctly.
		// Clear only after lifecycle_end so openclaw UUID → device runId mapping still succeeds.
		if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			flow.ClearTrace()
		}

	case "tool":
		toolName := payload.ToolName()
		toolArgs := payload.ToolArguments()
		summary := toolName
		if payload.Data.Phase == "start" {
			// Hardware-reaction tools soft-cancel any pending filler —
			// the user already perceives the device reacting. Non-HW
			// tools leave the timer running so the filler can fire
			// during a long Bash/curl/Read.
			sensinghttp.DefaultFillerManager.OnToolStart(flowRunID, toolArgs, toolName)
			summary = fmt.Sprintf("Tool %s started", toolName)
			// Detect music playback tool calls so we can suppress TTS on turn end.
			// The Music skill uses Bash+curl to POST /audio/play.
			if strings.Contains(toolArgs, "/audio/play") {
				h.suppressTTS(payload.RunID, "music_playing")
				slog.Info("music tool detected, TTS will be suppressed for this turn", "component", "agent", "runId", payload.RunID)
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
				// music.play logged via flow.Log above
			}
			// Emit specific hardware events for flow monitor visualization.
			// Both flow.Log (for JSONL persistence + UI flow_event triggers) and monitorBus (for SSE).
			if strings.Contains(toolArgs, "/emotion") {
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
			if strings.Contains(toolArgs, "/led/off") {
				h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "agent tool: " + toolName})
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_led", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
			}
			if strings.Contains(toolArgs, "/servo/aim") || strings.Contains(toolArgs, "/servo/play") {
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_servo", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
			}
			// Intercept OpenClaw built-in tts tool: extract text and route to HAL speaker.
			// The built-in tts generates audio server-side but never reaches the physical speaker.
			if toolName == "tts" {
				if ttsText := extractTTSText(toolArgs); ttsText != "" {
					isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
					isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
					isSilent := h.agentGateway.IsSilentRun(flowRunID)
					slog.Info("intercepted built-in tts tool, routing to HAL", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat, "silent", isSilent)
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
					if !isChannelRun && !isWebChat && !isSilent {
						go func(t string) {
							if err := h.agentGateway.SendToHALTTS(t); err != nil {
								slog.Error("TTS intercept delivery failed", "component", "agent", "error", err)
							}
						}(ttsText)
					}
					// Mark this turn as already spoken so lifecycle_end won't double-speak.
					h.suppressTTS(payload.RunID, "already_spoken")
				}
			}
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "result" {
			// Tool finished — re-arm the filler timer if the turn is
			// still active. Long multi-tool turns get a filler at each
			// dead-air pocket, capped by MaxFillersPerTurn and gated
			// by FillerCooldown.
			//
			// OpenClaw emits phase="result" for native tools (read,
			// web_search, web_fetch, exec, …) and phase="end" for
			// some legacy paths; both signal the same boundary. Until
			// 2026-05-12 this branch only matched "end", which meant
			// every native tool silently skipped the filler re-arm
			// and only the very first Continuation ever fired —
			// observable as "no filler during web_search" UX.
			sensinghttp.DefaultFillerManager.OnToolEnd(flowRunID)
			result := payload.ResultText()
			if len(result) > 100 {
				result = result[:100] + "..."
			}
			summary = fmt.Sprintf("Tool %s done", toolName)
			if result != "" {
				summary += ": " + result
			}
		}
		flow.Log("tool_call", map[string]any{"tool": toolName, "phase": payload.Data.Phase, "run_id": flowRunID}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "tool_call",
			Summary: summary,
			RunID:   flowRunID,
			Phase:   payload.Data.Phase,
			Detail: map[string]string{
				"tool": toolName,
				"args": toolArgs,
			},
		})

	case "thinking":
		delta := payload.Data.Delta
		if delta == "" {
			delta = payload.Data.Text
		}
		// Don't truncate deltas — they are merged in the frontend
		if delta != "" {
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "thinking",
				Summary: delta,
				RunID:   flowRunID,
			})
			if h.recordThinkingDelta(flowRunID, delta) {
				flow.Log("thinking_first_token", map[string]any{
					"run_id": flowRunID,
				}, flowRunID)
			}
		}

	case "assistant":
		delta := payload.Data.Delta
		if delta == "" {
			delta = payload.Data.Text
		}
		// Don't truncate deltas — they are merged in the frontend
		if delta != "" {
			// Real assistant text is streaming — hard-cancel any
			// pending or in-flight filler so the device doesn't talk
			// over the actual reply. Cancel is idempotent so calling
			// it on every delta is safe.
			sensinghttp.DefaultFillerManager.Cancel(flowRunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "assistant_delta",
				Summary: delta,
				RunID:   flowRunID,
			})
			if h.recordAssistantDelta(flowRunID, delta) {
				flow.Log("agent_first_token", map[string]any{
					"run_id": flowRunID,
				}, flowRunID)
			}
		}

		// When the agent turn ends, the final assistant text should be spoken.
		// Accumulate deltas per runId and send to TTS when lifecycle "end" arrives.
		h.accumulateAssistantDelta(payload.RunID, delta)

		// Slack (hermes HTTP bridge): stream the reply progressively into the live
		// Slack message (chat.appendStream). Feed the cleaned cumulative text so far;
		// the bridge throttles + appends the new tail. Guarded by the SlackBridge
		// type-assert + IsSlackOriginRun peek — non-Slack runs / openclaw are untouched.
		if sb, ok := h.agentGateway.(domain.SlackBridge); ok && sb.IsSlackOriginRun(flowRunID) {
			if clean, ready := h.cleanedSlackStreamText(payload.RunID); ready {
				sb.StreamSlackDelta(flowRunID, clean)
			}
		}

		// Sentence-streaming: dispatch the FIRST complete sentence to
		// /voice/speak as soon as the agent emits the boundary so the
		// device starts speaking before generation finishes. Only the
		// first sentence streams here — chaining every sentence as its
		// own POST exposes a per-sentence TTFB gap. Lifecycle:end sends
		// the remainder through /voice/speak-queue so Python pre-synths
		// it while sentence 1 plays and chains the rest seamlessly.
		if h.canStreamSentenceTTS(payload.RunID, flowRunID) {
			if sentence := h.tryFirstSentenceFlush(payload.RunID); sentence != "" {
				cleaned := sanitizeAgentText(sentence)
				if cleaned != "" {
					// ADDED 2026-05-26: fire leading HW markers SYNC before TTS POST so
					// state mutations (e.g. /scene/off → speaker unmute) apply in HAL
					// before /voice/speak-queue arrives. Without this, TTS races ahead
					// and gets rejected while speaker is still muted by a prior scene.
					// extractLeadingHWCalls only picks markers BEFORE first non-marker
					// text, so inline markers (between sentences) stay deferred to
					// lifecycle:end and preserve position-in-text semantics.
					h.assistantMu.Lock()
					buf := h.assistantBuf[payload.RunID]
					rawSnapshot := ""
					if buf != nil {
						rawSnapshot = buf.String()
					}
					h.assistantMu.Unlock()
					if rawSnapshot != "" {
						leading := extractLeadingHWCalls(rawSnapshot)
						if len(leading) > 0 {
							h.fireHWCallsSync(leading, flowRunID)
							h.recordFiredHWCount(payload.RunID, len(leading))
						}
					}
					slog.Info("streaming first sentence to TTS",
						"component", "agent",
						"run_id", flowRunID,
						"sentence", cleaned[:min(len(cleaned), 100)])
					flow.Log("tts_stream_send", map[string]any{"run_id": flowRunID, "text": cleaned}, flowRunID)
					go func(s string) {
						if err := h.agentGateway.SendToHALTTSQueue(s); err != nil {
							slog.Error("streaming TTS delivery failed", "component", "agent", "error", err)
						}
					}(cleaned)
				}
			}
		}

	}

	// When agent lifecycle ends, flush accumulated assistant text to TTS.
	// Suppress TTS if the agent played music or already spoke via tool intercept.
	if payload.Stream == "lifecycle" && payload.Data.Phase == "end" {
		// Persist streaming summary to JSONL. Raw deltas only live in
		// monitorBus (RAM) — Flow Monitor reads JSONL on reload, so
		// without these summary events the pipeline rect shows no
		// thinking/assistant rows for past turns. Mirror agent_thinking
		// which is similarly populated from chat.history at turn end.
		if s := h.drainStreamStats(flowRunID); s != nil {
			if s.thinkingChunks > 0 {
				flow.Log("thinking_last_token", map[string]any{
					"run_id": flowRunID,
					"text":   s.thinkingText.String(),
					"chunks": s.thinkingChunks,
					"chars":  s.thinkingChars,
				}, flowRunID)
			}
			if s.assistantChunks > 0 {
				flow.Log("agent_last_token", map[string]any{
					"run_id": flowRunID,
					"text":   s.assistantText.String(),
					"chunks": s.assistantChunks,
					"chars":  s.assistantChars,
				}, flowRunID)
			}
		}

		// Hard-cancel any lingering filler before the real TTS flush
		// — covers edge case where the turn ended without any
		// assistant delta (NO_REPLY, HW-only reply, error).
		sensinghttp.DefaultFillerManager.Cancel(flowRunID)
		suppressReason := h.clearTTSSuppress(payload.RunID)
		// Pull interleaved Telegram DM target up front so the map entry is
		// cleared even on NO_REPLY / HW-only / suppressed branches that
		// never reach the dmTelegramID injection below.
		interleavedDMTarget := h.consumeInterleavedDM(payload.RunID)
		if interleavedDMTarget == "" {
			interleavedDMTarget = h.consumeInterleavedDM(flowRunID)
		}
		// Web monitor chat: suppress TTS — response displayed in web UI only.
		if suppressReason == "" && h.agentGateway.ConsumeWebChatRun(flowRunID) {
			suppressReason = "web_chat"
		}
		// Realtime voice agent already spoke this turn (voice_agent_handled):
		// suppress TTS so OpenClaw's reply isn't double-spoken.
		if suppressReason == "" && h.agentGateway.ConsumeSilentRun(flowRunID) {
			suppressReason = "voice_agent_handled"
		}
		text, hwCalls := h.flushAssistantText(payload.RunID)
		// streamedCleanLen > 0 means the first sentence was dispatched
		// mid-turn via tryFirstSentenceFlush; the remainder TTS POST
		// below slices `text` at that offset to skip what already
		// played. Broadcast/DM still use full `text` since it covers
		// the entire reply the user heard.
		streamedLen := h.consumeStreamedCleanLen(payload.RunID)
		if streamedLen > len(text) {
			streamedLen = len(text)
		}
		streamed := streamedLen > 0
		// ADDED 2026-05-26: skip leading HW markers already fired at stream-time
		// (see fireHWCallsSync call earlier). extractHWCalls returns markers in
		// stable source order, so [firedAtStream:] is the remainder.
		firedAtStream := h.consumeFiredHWCount(payload.RunID)
		if firedAtStream > len(hwCalls) {
			firedAtStream = len(hwCalls)
		}
		hwCalls = hwCalls[firedAtStream:]
		if text != "" || len(hwCalls) > 0 || streamed {
			// ADDED 2026-05-27: fire SYNC (was h.fireHWCalls async) so state-
			// changing markers like /scene/off apply before the lifecycle-end
			// TTS POST below races ahead and gets rejected on still-muted
			// speaker. Single-sentence responses skip stream-time fire (no
			// sentence boundary) — without sync here, the race returns.
			// fireHWCallsSync has 100ms per-call timeout + async fallback,
			// so heavy markers (e.g. /servo/track) don't block TTS.
			h.fireHWCallsSync(hwCalls, flowRunID)

			// [2026-05-11] DISABLED — TTS suppress on /audio/play was killing the
			// agent's main reply (e.g. "Mình chọn River Flows in You…") and
			// leaving only the random short backchannel cue. Python music_service
			// already waits for TTS via wait_for_tts() before grabbing ALSA, so
			// this Go-side suppress is redundant. Rollback: uncomment to restore
			// hard-suppress behavior.
			// if suppressReason == "" {
			// 	for _, c := range hwCalls {
			// 		if strings.Contains(c.path, "/audio/play") {
			// 			suppressReason = "music_playing"
			// 			break
			// 		}
			// 	}
			// }

			// Consume broadcast marker early to prevent map leak on NO_REPLY/empty/suppressed paths.
			isBroadcastRun := h.agentGateway.ConsumeBroadcastRun(flowRunID)

			// Slack (hermes HTTP bridge): a Slack-origin turn replies in Slack with TTS
			// suppressed. Peek (don't consume) so the mid-turn streaming gate can also
			// see it; DeliverSlackReply consumes at reply time. isSlackRun is false for
			// every non-Slack run and every runtime that isn't a SlackBridge (openclaw).
			slackBridge, _ := h.agentGateway.(domain.SlackBridge)
			isSlackRun := slackBridge != nil && slackBridge.IsSlackOriginRun(flowRunID)

			// [HW:/broadcast] marker: fan-out reply text to all Telegram chats (guard-only).
			// [HW:/speak] marker: force TTS on the speaker without any channel fan-out —
			// used by proactive triggers (e.g. music suggestions) that run inside a
			// channel session but need to speak out loud anyway.
			// [HW:/dm:{"telegram_id":"123"}] marker: send reply to a specific Telegram user.
			var dmTelegramID string
			forceTTS := false
			for _, c := range hwCalls {
				if c.path == "/broadcast" {
					isBroadcastRun = true
				}
				if c.path == "/speak" {
					forceTTS = true
				}
				if c.path == "/dm" {
					var dm struct {
						TelegramID string `json:"telegram_id"`
					}
					if err := json.Unmarshal([]byte(c.body), &dm); err == nil && dm.TelegramID != "" {
						dmTelegramID = dm.TelegramID
					}
				}
			}
			// Queue-mode interleave: when the agent didn't include a /dm
			// marker but a Telegram message was injected mid-turn, route
			// the reply back to the originating chat (captured from
			// session.message metadata in the lifecycle window).
			if dmTelegramID == "" && interleavedDMTarget != "" {
				dmTelegramID = interleavedDMTarget
				slog.Info("routing reply to interleaved Telegram chat (queue-mode injection)",
					"component", "agent", "run_id", flowRunID, "chat_id", dmTelegramID)
			}

			// Guard mode: broadcast even on NO_REPLY / empty / suppressed paths.
			// The agent may choose not to speak, but we still want to alert the owner via Telegram.
			if snap, ok := h.agentGateway.ConsumeGuardRun(flowRunID); ok {
				guardText := text
				if guardText == "" || isAgentNoReply(guardText) {
					guardText = "Motion or presence detected while guard mode is active."
				}
				go func(t, s string) {
					slog.Info("guard broadcast via Telegram Bot API", "component", "agent", "run_id", flowRunID, "text", t[:min(len(t), 80)])
					if err := h.agentGateway.Broadcast(t, s); err != nil {
						slog.Error("guard broadcast failed", "component", "agent", "err", err)
					}
				}(guardText, snap)
			}

			// Detect heartbeat before sanitizing strips the sentinel.
			isHeartbeatRun := strings.Contains(strings.ToUpper(text), "HEARTBEAT_OK")
			// Extract <say>...</say> wrapper if the skill uses it (wellbeing).
			// Non-tagged replies pass through unchanged.
			text = extractSayTag(text)
			text = sanitizeAgentText(text)
			// Slice off the prefix already streamed mid-turn so the
			// remainder POST doesn't replay sentence 1. Clamp because
			// extractSayTag / sanitizeAgentText may shorten text below
			// the previously-tracked offset.
			if streamedLen > len(text) {
				streamedLen = len(text)
			}
			remainderText := strings.TrimSpace(text[streamedLen:])
			if isAgentNoReply(text) {
				// NO_REPLY in remainder. If streamed > 0 the agent
				// already spoke sentence 1; can't unspeak it. Log a
				// warning so we notice any skill that mixes NO_REPLY
				// with real speech.
				if streamed {
					slog.Warn("NO_REPLY in remainder after first sentence streamed",
						"component", "agent", "run_id", flowRunID, "streamed_len", streamedLen)
				} else {
					slog.Info("agent replied NO_REPLY, skipping TTS", "component", "agent", "run_id", flowRunID)
				}
				flow.Log("no_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "chat_response",
					Summary: "[no reply]",
					RunID:   flowRunID,
					State:   "final",
					Detail:  map[string]string{"role": "assistant", "message": "[no reply]"},
				})
			} else if remainderText == "" {
				if streamed {
					// Reply was a single sentence already streamed
					// mid-turn — nothing left to TTS at end. Log so
					// the flow monitor shows turn complete instead
					// of a misleading hw_only_reply.
					slog.Info("assistant turn complete via first-sentence streaming",
						"component", "agent", "run_id", flowRunID, "streamed_len", streamedLen)
					flow.Log("tts_stream_complete", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
				} else {
					// HW-only reply (only markers, no spoken text)
					flow.Log("hw_only_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				}
			} else if suppressReason != "" {
				slog.Info("assistant turn done, TTS suppressed", "component", "agent", "reason", suppressReason, "text", text[:min(len(text), 100)])
				flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppressReason, "text": text}, flowRunID)
			} else {
				// Channel detection: positive-evidence only. tg- runIDs are
				// synthesised by the device from session.message events (real Telegram
				// users); anything else (device-chat-*, UUID from steer/cron/
				// heartbeat) is NOT a channel run unless explicitly marked
				// via channelRuns below.
				//
				// Previously this defaulted to `!isDeviceOutboundChatRunID(...)`,
				// which mis-classified OpenClaw UUID self-fire / cron / heartbeat
				// runs as Telegram and suppressed their TTS — most visibly,
				// music-suggestion replies on emotion.detected events when the
				// sensing turn got steered into a UUID host turn.
				isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
				// Cron-fire turns always TTS on the device speaker even though their
				// UUID runIds look like channel runs. Detected from chat.history
				// systemEvent template at lifecycle_start (see cronFireRuns map).
				h.cronFireRunsMu.Lock()
				isCronFire := h.cronFireRuns[payload.RunID] || h.cronFireRuns[flowRunID]
				delete(h.cronFireRuns, payload.RunID)
				delete(h.cronFireRuns, flowRunID)
				h.cronFireRunsMu.Unlock()
				if isCronFire {
					isChannelRun = false
				}
				// [HW:/broadcast] (guard) or [HW:/speak] (proactive crons) force TTS
				// even for channel-origin runs.
				if isBroadcastRun || forceTTS {
					isChannelRun = false
				}
				// Heartbeat cron responses must never reach the speaker.
				if isHeartbeatRun {
					isChannelRun = true
				}
				// Override: confirmed channel turn via senderLabel always suppresses TTS.
				// Covers race where Telegram UUID mapped to sensing trace (device-sensing-*).
				h.channelRunsMu.Lock()
				if h.channelRuns[payload.RunID] || h.channelRuns[flowRunID] {
					isChannelRun = true
				}
				delete(h.channelRuns, payload.RunID)
				delete(h.channelRuns, flowRunID)
				h.channelRunsMu.Unlock()
				// A Slack-origin turn replies in Slack, never on the speaker.
				if isSlackRun {
					isChannelRun = true
				}
				if isChannelRun {
					// TTS would be gated by channel_run — log suppression so the
					// monitor doesn't misleadingly show a "tts_send" event when the
					// speaker stays silent. Channel/Telegram users still receive
					// the text via OpenClaw's own session fan-out.
					slog.Info("assistant turn done, TTS suppressed (channel run)", "component", "agent", "text", text[:min(len(text), 100)], "broadcast", isBroadcastRun, "force_tts", forceTTS, "cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": "channel_run", "text": text}, flowRunID)
				} else {
					// remainderText excludes the first sentence already
					// streamed (when streamed=true). Use /voice/speak-queue
					// so Python pre-synthesises while sentence 1 is still
					// playing and chains the remainder seamlessly onto
					// the open ALSA stream (no inter-sentence gap). Non-
					// streamed turns also go through the queue endpoint
					// — when idle it behaves exactly like /voice/speak,
					// so this is a safe drop-in.
					slog.Info("assistant turn done, sending to TTS",
						"component", "agent",
						"text", remainderText[:min(len(remainderText), 100)],
						"streamed_len", streamedLen,
						"broadcast", isBroadcastRun, "force_tts", forceTTS,
						"cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					// `text` is the FULL reply (sentence 1 + remainder); `remainderText`
					// excludes sentence 1 when it was streamed mid-turn via
					// tts_stream_send. Carry `full_text` so the web (chat + flow turn)
					// can display the complete reply — it only reads tts_send and would
					// otherwise drop sentence 1 (logged separately as tts_stream_send).
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": remainderText, "full_text": text, "streamed_len": streamedLen}, flowRunID)
					go func(t string) {
						if err := h.agentGateway.SendToHALTTSQueue(t); err != nil {
							slog.Error("TTS delivery failed", "component", "agent", "error", err)
						}
					}(remainderText)
				}
				// Guard broadcast is handled above (before the if/else) to ensure
				// it fires even on NO_REPLY / empty / suppressed paths.
				// DM run: send agent response to a specific Telegram user.
				// Takes priority over broadcast — if /dm is present, /broadcast is skipped.
				if dmTelegramID != "" && len(text) > 10 {
					// Auto-attach the worst pose frames when this turn was
					// triggered by a motion.activity that surfaced a posture
					// nudge. Mirrors the guard-snapshot path: agent doesn't
					// know any file paths — the device resolved them at ingest.
					poseBucket, poseFiles, hasPoseBucket := h.agentGateway.ConsumePoseBucketRun(flowRunID)
					var poseImagePaths []string
					if hasPoseBucket {
						poseImagePaths = buildPoseBucketImagePaths(poseBucket, poseFiles)
						slog.Info("dm attaching pose bucket images",
							"component", "agent", "run_id", flowRunID,
							"bucket", poseBucket, "count", len(poseImagePaths))
					}
					go func(t, tid string, paths []string) {
						slog.Info("dm run response to user", "component", "agent", "run_id", flowRunID, "telegram_id", tid, "text", t[:min(len(t), 80)], "images", len(paths))
						if len(paths) > 0 {
							if err := h.agentGateway.SendToUserWithMedia(tid, t, paths); err != nil {
								slog.Error("dm run with media failed", "component", "agent", "err", err)
							}
							return
						}
						if err := h.agentGateway.SendToUser(tid, t, ""); err != nil {
							slog.Error("dm run failed", "component", "agent", "err", err)
						}
					}(text, dmTelegramID, poseImagePaths)
				} else if isBroadcastRun && len(text) > 10 {
					// Broadcast run (e.g. music.mood): send agent response to all channels
					// so user can confirm via Telegram instead of only voice.
					go func(t string) {
						slog.Info("broadcast run response to channels", "component", "agent", "run_id", flowRunID, "text", t[:min(len(t), 80)])
						if err := h.agentGateway.Broadcast(t, ""); err != nil {
							slog.Error("broadcast run failed", "component", "agent", "err", err)
						}
					}(text)
				}
			}

			// Slack (hermes HTTP bridge): finalize the turn for EVERY end-phase outcome
			// (real reply, NO_REPLY, suppressed, empty), not just the spoken-reply branch
			// above — otherwise the per-run stream goroutine + origin/stream maps leak.
			// DeliverSlackReply consumes the origin, stops the stream (chat.stopStream),
			// and posts nothing when the text is empty. NO_REPLY → deliver "" (cleanup).
			if isSlackRun && slackBridge != nil {
				slackText := text
				if isAgentNoReply(text) {
					slackText = ""
				}
				go func(t string) {
					if err := slackBridge.DeliverSlackReply(flowRunID, t); err != nil {
						slog.Error("slack reply failed", "component", "agent", "err", err)
					}
				}(slackText)
			}
		}
	}

	return nil
}
