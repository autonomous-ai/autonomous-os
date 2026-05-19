package sse

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/lib/flow"
	sensinghttp "go-lamp.autonomous.ai/server/sensing/delivery/http"
)

// HandleEvent processes incoming WebSocket events from the OpenClaw gateway.
func (h *OpenClawHandler) HandleEvent(ctx context.Context, evt domain.WSEvent) error {
	slog.Debug("event received", "component", "agent", "event", evt.Event)

	// OpenClaw cron events: action="started" fires immediately before the
	// agent lifecycle_start for a cron-triggered turn. Payload schema (from
	// src/cron/service/state.ts CronEvent): { jobId, action, sessionKey,
	// runAtMs, ... }. We cache sessionKey → timestamp; the next lifecycle_start
	// matching that sessionKey within cronFireWindowMs gets marked as a cron
	// fire so isChannelRun is overridden and TTS reaches the lamp speaker.
	if evt.Event == "cron" {
		// Diagnostic: dump raw cron payload — keep until correlation is proven
		// stable across all sessionTarget variants.
		slog.Info("cron event raw payload", "component", "agent", "payload", string(evt.Payload))
		var cronEvt struct {
			Action  string `json:"action"`
			JobID   string `json:"jobId"`
			RunAtMs int64  `json:"runAtMs"`
		}
		if err := json.Unmarshal(evt.Payload, &cronEvt); err == nil && cronEvt.Action == "started" {
			now := time.Now().UnixMilli()
			h.cronFireExpectedMu.Lock()
			// Prune stale entries before pushing — bounds queue growth.
			cutoff := now - cronFireWindowMs
			pruned := h.cronFireExpected[:0]
			for _, ts := range h.cronFireExpected {
				if ts >= cutoff {
					pruned = append(pruned, ts)
				}
			}
			h.cronFireExpected = append(pruned, now)
			h.cronFireExpectedMu.Unlock()
			slog.Info("cron started — expecting lifecycle_start", "component", "agent", "job_id", cronEvt.JobID, "run_at_ms", cronEvt.RunAtMs)
		}
	}

	switch evt.Event {
	case "agent":
		var payload domain.AgentPayload
		if err := json.Unmarshal(evt.Payload, &payload); err != nil {
			return err
		}
		// Capture session key from any agent event
		if payload.SessionKey != "" && h.agentGateway.GetSessionKey() == "" {
			h.agentGateway.SetSessionKey(payload.SessionKey)
		}

		// Map OpenClaw UUID → device idempotencyKey on lifecycle_start.
		// Only map when the lifecycle belongs to Lumi's own direct session — group/channel
		// sessions have independent runs that must NOT be merged into sensing traces.
		//
		// Two paths depending on payload.RunID format:
		//   • Lumi-format (lumi-chat-*): OpenClaw 5.4+ echoes the idempotencyKey as
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
		lumiSession := h.agentGateway.GetSessionKey()
		isLumiSession := lumiSession != "" && payload.SessionKey == lumiSession
		if payload.Stream == "lifecycle" && payload.Data.Phase == "start" && payload.RunID != "" && isLumiSession {
			if isLumiOutboundChatRunID(payload.RunID) {
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
			// (no lumi- prefix) so chat.send/sensing turns can't accidentally
			// claim a queued cron slot.
			if payload.Data.Phase == "start" && payload.RunID != "" && !isLumiOutboundChatRunID(payload.RunID) {
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
			// with a UUID run_id (not lumi-chat-* prefix). This covers:
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
				!isLumiOutboundChatRunID(payload.RunID) && !isLumiOutboundChatRunID(flowRunID) &&
				!isCronFireTurn
			if isChannelTurn {
				// Emit chat_input immediately so UI shows turn-started.
				// Use a neutral "[chat]" placeholder rather than claiming the
				// configured channel — the goroutine below will replace this
				// with the right label ([telegram:Gray] / [voice] / [emotion]
				// / ...) once chat.history reveals whether it's a real
				// channel user or a Lumi-internal sensing/voice merge. If
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
					// makes flowRunID = lumi-sensing-* → isChannelRun wrongly false.
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
						//  2. Lumi-internal sensing/voice/wellbeing/system message
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
							if lbl := labelForLumiInternal(userMsg); lbl != "" {
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
			// Only gate on lifecycles that belong to a Lumi-initiated turn — these are
			// the only ones whose `end` is reliably round-tripped through SSE.
			// Heartbeat (target:"none"), channel turns merged by steer mode, and other
			// OpenClaw self-trigger lifecycles can drop their `end` SSE (per the
			// busyTTL comment in service_events.go); gating on them strands activeTurn=true
			// for up to 5 minutes — every Lumi sensing event in that window queues
			// instead of forwarding.
			//
			// External turns don't NEED Lumi-side gating: with messages.queue.mode=steer
			// (pinned in onboarding), concurrent sensing events arriving during a
			// channel/cron turn are batched into the active turn at the next model
			// boundary by OpenClaw itself — no need for Lumi to pre-suppress them.
			//
			// Lumi-initiated turns also flip activeTurn=true at chat.send time
			// (service_chat.go), so a missed lifecycle.start here is harmless.
			// LED is managed by the agent via /emotion skill calls — do not override here.
			if payload.Data.Phase == "start" {
				lumiInitiated := isLumiOutboundChatRunID(payload.RunID) || isLumiOutboundChatRunID(flowRunID)
				if lumiInitiated {
					h.agentGateway.SetBusy(true)
				} else {
					slog.Info("lifecycle.start skipped for busy gating",
						"component", "agent", "run_id", payload.RunID, "flow_run_id", flowRunID,
						"reason", "not lumi-initiated — heartbeat/channel/cron handled by OpenClaw steer batching")
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
						"run_id":            flowRunID,
						"input_tokens":      u.InputTokens,
						"output_tokens":     u.OutputTokens,
						"cache_read_tokens": u.CacheReadTokens,
						"cache_write_tokens": u.CacheWriteTokens,
						"total_tokens":      u.TotalTokens,
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
								Role    string         `json:"role"`
								Usage   *histUsage     `json:"usage,omitempty"`
								Content []histContent  `json:"content,omitempty"`
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
											"run_id":  capturedFlowRunID,
											"source":  "chat_history",
											"text":    c.Thinking,
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
								// history but keeps Lumi external memory (mood/habit/owner).
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
				// the user already perceives the lamp reacting. Non-HW
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
				// Intercept OpenClaw built-in tts tool: extract text and route to LeLamp speaker.
				// The built-in tts generates audio server-side but never reaches the physical speaker.
				if toolName == "tts" {
					if ttsText := extractTTSText(toolArgs); ttsText != "" {
						isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
						isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
						slog.Info("intercepted built-in tts tool, routing to LeLamp", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat)
						flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
						if !isChannelRun && !isWebChat {
							go func(t string) {
								if err := h.agentGateway.SendToLeLampTTS(t); err != nil {
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
			}

		case "assistant":
			delta := payload.Data.Delta
			if delta == "" {
				delta = payload.Data.Text
			}
			// Don't truncate deltas — they are merged in the frontend
			if delta != "" {
				// Real assistant text is streaming — hard-cancel any
				// pending or in-flight filler so the lamp doesn't talk
				// over the actual reply. Cancel is idempotent so calling
				// it on every delta is safe.
				sensinghttp.DefaultFillerManager.Cancel(flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "assistant_delta",
					Summary: delta,
					RunID:   flowRunID,
				})
			}

			// When the agent turn ends, the final assistant text should be spoken.
			// Accumulate deltas per runId and send to TTS when lifecycle "end" arrives.
			h.accumulateAssistantDelta(payload.RunID, delta)

		}

		// When agent lifecycle ends, flush accumulated assistant text to TTS.
		// Suppress TTS if the agent played music or already spoke via tool intercept.
		if payload.Stream == "lifecycle" && payload.Data.Phase == "end" {
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
			if text, hwCalls := h.flushAssistantText(payload.RunID); text != "" || len(hwCalls) > 0 {
				// Fire HW calls with full tracking (flow.Log + lastEmotion + monitorBus).
				h.fireHWCalls(hwCalls, flowRunID)

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
				if isAgentNoReply(text) {
					// NO_REPLY: agent explicitly decided to do nothing
					slog.Info("agent replied NO_REPLY, skipping TTS", "component", "agent", "run_id", flowRunID)
					flow.Log("no_reply", map[string]any{"run_id": flowRunID}, flowRunID)
					h.monitorBus.Push(domain.MonitorEvent{
						Type:    "chat_response",
						Summary: "[no reply]",
						RunID:   flowRunID,
						State:   "final",
						Detail:  map[string]string{"role": "assistant", "message": "[no reply]"},
					})
				} else if text == "" {
					// HW-only reply (only markers, no spoken text)
					flow.Log("hw_only_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				} else if suppressReason != "" {
					slog.Info("assistant turn done, TTS suppressed", "component", "agent", "reason", suppressReason, "text", text[:min(len(text), 100)])
					flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppressReason, "text": text}, flowRunID)
				} else {
					// Channel detection: positive-evidence only. tg- runIDs are
					// synthesised by Lumi from session.message events (real Telegram
					// users); anything else (lumi-chat-*, UUID from steer/cron/
					// heartbeat) is NOT a channel run unless explicitly marked
					// via channelRuns below.
					//
					// Previously this defaulted to `!isLumiOutboundChatRunID(...)`,
					// which mis-classified OpenClaw UUID self-fire / cron / heartbeat
					// runs as Telegram and suppressed their TTS — most visibly,
					// music-suggestion replies on emotion.detected events when the
					// sensing turn got steered into a UUID host turn.
					isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
					// Cron-fire turns always TTS on the lamp speaker even though their
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
					// Covers race where Telegram UUID mapped to sensing trace (lumi-sensing-*).
					h.channelRunsMu.Lock()
					if h.channelRuns[payload.RunID] || h.channelRuns[flowRunID] {
						isChannelRun = true
					}
					delete(h.channelRuns, payload.RunID)
					delete(h.channelRuns, flowRunID)
					h.channelRunsMu.Unlock()
					if isChannelRun {
						// TTS would be gated by channel_run — log suppression so the
						// monitor doesn't misleadingly show a "tts_send" event when the
						// speaker stays silent. Channel/Telegram users still receive
						// the text via OpenClaw's own session fan-out.
						slog.Info("assistant turn done, TTS suppressed (channel run)", "component", "agent", "text", text[:min(len(text), 100)], "broadcast", isBroadcastRun, "force_tts", forceTTS, "cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
						flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": "channel_run", "text": text}, flowRunID)
					} else {
						slog.Info("assistant turn done, sending to TTS", "component", "agent", "text", text[:min(len(text), 100)], "broadcast", isBroadcastRun, "force_tts", forceTTS, "cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
						flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
						go func(t string) {
							if err := h.agentGateway.SendToLeLampTTS(t); err != nil {
								slog.Error("TTS delivery failed", "component", "agent", "error", err)
							}
						}(text)
					}
					// Guard broadcast is handled above (before the if/else) to ensure
					// it fires even on NO_REPLY / empty / suppressed paths.
					// DM run: send agent response to a specific Telegram user.
					// Takes priority over broadcast — if /dm is present, /broadcast is skipped.
					if dmTelegramID != "" && len(text) > 10 {
						go func(t, tid string) {
							slog.Info("dm run response to user", "component", "agent", "run_id", flowRunID, "telegram_id", tid, "text", t[:min(len(t), 80)])
							if err := h.agentGateway.SendToUser(tid, t, ""); err != nil {
								slog.Error("dm run failed", "component", "agent", "err", err)
							}
						}(text, dmTelegramID)
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
			}
		}

	case "session.tool":
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
			if strings.Contains(toolArgs, "/audio/play") {
				h.suppressTTS(payload.RunID, "music_playing")
				slog.Info("music tool detected (session.tool), TTS suppressed", "component", "agent", "runId", payload.RunID)
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
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
			// Intercept OpenClaw built-in tts tool (session.tool path).
			if toolName == "tts" {
				if ttsText := extractTTSText(toolArgs); ttsText != "" {
					isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
					isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
					slog.Info("intercepted built-in tts tool (session.tool), routing to LeLamp", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat)
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
					if !isChannelRun && !isWebChat {
						go func(t string) {
							if err := h.agentGateway.SendToLeLampTTS(t); err != nil {
								slog.Error("TTS intercept delivery failed", "component", "agent", "error", err)
							}
						}(ttsText)
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
		flow.Log("tool_call", map[string]any{"tool": toolName, "phase": payload.Data.Phase, "run_id": flowRunID, "source": "session.tool", "args": toolArgs}, flowRunID)
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

	case "chat":
		slog.Debug("chat raw payload", "component", "agent", "payload", string(evt.Payload))
		var payload domain.ChatPayload
		if err := json.Unmarshal(evt.Payload, &payload); err != nil {
			slog.Error("chat parse error", "component", "agent", "error", err, "raw", string(evt.Payload))
			return nil
		}
		payload.ResolveChatMessage()
		slog.Info(">>> CHAT EVENT RECEIVED", "component", "agent",
			"run_id", payload.RunID,
			"role", payload.Role,
			"state", payload.State,
			"message_len", len(payload.Message),
			"message", payload.Message,
			"raw_message", string(payload.RawMessage))
		// Same as agent stream: OpenClaw may send UUID while lifecycle/tool/tts used resolved device id.
		flowRunID := h.resolveRunID(payload.RunID)
		// Debug alignment: OpenClaw "chat" stream may or may not include user messages for outbound chat.send.
		// When flowRunID belongs to Lumi, log role/state/message so we can confirm whether chat_input can be emitted.
		if strings.HasPrefix(flowRunID, "lumi-") {
			msgPreview := payload.Message
			msgPreview = strings.ReplaceAll(msgPreview, "\n", " ")
			if len(msgPreview) > 120 {
				msgPreview = msgPreview[:120] + "…"
			}
			slog.Info("openclaw chat event (lumi)", "component", "agent",
				"openclaw_run_id", payload.RunID,
				"flow_run_id", flowRunID,
				"role", payload.Role,
				"state", payload.State,
				"has_message", strings.TrimSpace(msgPreview) != "",
				"message_preview", msgPreview)
		}
		if payload.RunID != "" && flowRunID != payload.RunID {
			slog.Info("flow correlation", "op", "chat_run_resolve", "section", "openclaw_chat",
				"openclaw_run_id", payload.RunID, "device_run_id", flowRunID,
				"role", payload.Role, "state", payload.State)
		}


		// (OpenClaw gateway never broadcasts role:"user" on the chat stream.
		// User messages are captured via lifecycle_start + chat.history above.)

		// Chat error: OpenClaw reports agent processing failure
		if payload.State == "error" {
			errMsg := payload.ErrorMessage
			if errMsg == "" {
				errMsg = "unknown error"
			}
			slog.Error("OpenClaw chat error", "component", "agent", "run_id", flowRunID, "error", errMsg)
			flow.Log("agent_error", map[string]any{"run_id": flowRunID, "error": errMsg}, flowRunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: "❌ " + shortError(errMsg),
				RunID:   flowRunID,
				State:   "error",
				Error:   shortError(errMsg),
				Detail:  map[string]string{"error": shortError(errMsg)},
			})
		}

		// Factual detection: OpenClaw sent a `state:"final"` chat event with
		// empty Message for a Lumi-format runId, and Lumi never opened a
		// lifecycle for that runId (pendingChatTrace entry still present —
		// lifecycle_start would have removed it; see ~line 84).
		//
		// We record only what we observe; we do NOT infer "steered" /
		// "merged" / "self-reply" — those are downstream interpretations
		// the operator makes from the timeline (e.g. a UUID lifecycle
		// arriving later with matching input).
		isLumiOutboundFinal := payload.State == "final" && isLumiOutboundChatRunID(flowRunID)
		isEmptyFinalNoLifecycle := isLumiOutboundFinal &&
			strings.TrimSpace(payload.Message) == "" &&
			h.agentGateway.RemovePendingChatTraceByRunID(flowRunID)
		if isEmptyFinalNoLifecycle {
			slog.Info("chat final empty, no lifecycle for runId",
				"component", "agent", "run_id", flowRunID)
			flow.Log("chat_final_empty", map[string]any{
				"run_id":            flowRunID,
				"state":             "final",
				"message_empty":     true,
				"lifecycle_started": false,
			}, flowRunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: "(empty final, no lifecycle)",
				RunID:   flowRunID,
				State:   "final",
				Detail: map[string]string{
					"message_empty":     "true",
					"lifecycle_started": "false",
				},
			})
			// No lifecycle.end will fire for this run — release the busy flag
			// here so subsequent sensing/chat events aren't queued for the
			// full busyTTL (5 min). chat.send sets activeTurn=true before
			// every write, including slash commands and steered/merged turns
			// that resolve via this empty-final path.
			h.agentGateway.SetBusy(false)
		}

		// Slash commands (e.g. /status, /new) are pre-LLM dispatched by OpenClaw
		// so they emit `state:"final"` with the reply payload but never open a
		// lifecycle. Without a closing flow event, Flow Monitor renders the turn
		// as active forever. Mirror chat_final_empty but record the success case.
		// RemovePendingChatTraceByRunID is the no-lifecycle witness: lifecycle_start
		// removes the entry, so a returning true here proves no lifecycle ran.
		// The existing isEmptyFinalNoLifecycle check above already consumed the
		// pending entry when it fires, so this Remove call is naturally false
		// when both conditions could match — no double-emit possible.
		isSlashFinalOk := isLumiOutboundFinal &&
			!isEmptyFinalNoLifecycle &&
			strings.TrimSpace(payload.Message) != "" &&
			h.agentGateway.RemovePendingChatTraceByRunID(flowRunID)
		if isSlashFinalOk {
			slog.Info("chat final ok, no lifecycle for runId (slash dispatcher)",
				"component", "agent", "run_id", flowRunID)
			// Include the reply payload (truncated like chat_input) so Flow
			// Monitor can render OUT for slash turns — without it, turnIO
			// has no source for the output field on these no-lifecycle turns.
			msgPreview := payload.Message
			if len(msgPreview) > 500 {
				msgPreview = msgPreview[:500] + "…"
			}
			flow.Log("chat_final_ok", map[string]any{
				"run_id":            flowRunID,
				"state":             "final",
				"message_empty":     false,
				"lifecycle_started": false,
				"message":           msgPreview,
			}, flowRunID)
			// Slash commands bypass the LLM lifecycle so lifecycle.end never
			// fires for this run. Without this, every /status (or /new etc.)
			// wedges the busy flag for the full busyTTL (5 min), queueing
			// every subsequent sensing/chat event behind it.
			h.agentGateway.SetBusy(false)
		}

		// Push assistant/partial chat events to monitor (user input tracked via lifecycle_start — already tracked as chat_input).
		// Skip the generic empty-final emit when we already pushed the factual chat_final_empty event above.
		if payload.Role != "user" && payload.State != "error" && !isEmptyFinalNoLifecycle {
			summary := payload.Message
			if len(summary) > 120 {
				summary = summary[:120] + "..."
			}
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: summary,
				RunID:   flowRunID,
				State:   payload.State,
				Detail: map[string]string{
					"role":    payload.Role,
					"message": payload.Message,
				},
			})
		}

		// TTS is sent from the lifecycle_end path above (assistant delta accumulation).
		// The chat stream's final message is not used for TTS to avoid speaking responses twice.

	case "session.message":
		// OpenClaw 5.x gates the `agent` lifecycle stream behind
		// isControlUiVisible (server-chat.ts), so non-Lumi-originated runs
		// (Telegram, etc.) emit no lifecycle_start/end on the agent path.
		// Drive chat_input + HW marker firing for those turns from
		// `session.message` instead. Lumi's own chat.send flows still use
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
		// reply reaches the lamp speaker, not just Telegram.
		if sm.Session.Origin.Provider == "heartbeat" {
			break
		}
		// Detect inbound channel turns. sessionKey prefix is the most stable
		// signal across OpenClaw versions; origin.provider/deliveryContext
		// are best-effort (sessionRow.origin can be undefined when telegram
		// routes through the default agent session).
		isTelegramChannel := strings.HasPrefix(sm.SessionKey, "agent:main:telegram:") ||
			sm.Session.Origin.Provider == "telegram" ||
			sm.Session.DeliveryContext.Channel == "telegram"
		if !isTelegramChannel {
			break
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
			// Lumi-issued run (sensing/voice chat.send) is being processed.
			// OpenClaw injects it into the running turn and the agent's reply
			// goes back on the Lumi run's stream — its runID is "lumi-chat-*"
			// so isLumiOutboundChatRunID() is true → isChannelRun=false →
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
				// Skip Lumi's own outbound echoes. Origin.Provider on a shared
				// `agent:main:main` session goes "sticky telegram" after any
				// real Telegram turn, so subsequent Lumi-issued chat.send
				// echoes (sensing/voice/wakeup) would otherwise look like
				// Telegram messages and falsely DM the last seen chat_id.
				// Two-layer check: prefix match (deterministic, survives the
				// 30s/32-entry buffer overflow) + IsRecentOutboundChat (catches
				// custom message texts not in the prefix list).
				msgText := extractMessageContentText(sm.Message.Content)
				if msgText != "" && (isLumiInternalMessage(msgText) || h.agentGateway.IsRecentOutboundChat(msgText)) {
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
			break
		}
		// Skip echoes of Lumi's own chat.send messages. session.message
		// arrives BEFORE the corresponding agent lifecycle.start (race), so
		// the lifecycle window above doesn't catch the first turn frame.
		// Match by exact text Lumi pushed via markOutboundChat (in sendChat),
		// plus a deterministic prefix check so burst voice/sensing turns that
		// overflow the 32-entry recent-outbound buffer or arrive >30s late
		// still get correctly classified as Lumi-internal (not Telegram).
		if sm.Message.Role == "user" {
			text := extractMessageContentText(sm.Message.Content)
			if text != "" && (isLumiInternalMessage(text) || h.agentGateway.IsRecentOutboundChat(text)) {
				slog.Info("session.message skipped — Lumi-outbound echo",
					"component", "agent", "sessionKey", sm.SessionKey,
					"preview", text[:min(len(text), 80)])
				break
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
			// (per-sender mode), so Lumi must DM via Bot API itself. Two
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
			slog.Info("channel turn started (session.message)", "component", "agent",
				"session_key", sm.SessionKey, "run_id", runID,
				"sender", senderLabel, "msg_preview", displayMsg)
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
			break
		}

		if sm.Message.Role != "assistant" {
			break
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
			break
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
			break
		}

		fullText = prunedImageMarkerRe.ReplaceAllString(fullText, "")
		hwCalls, cleanText := extractHWCalls(fullText)
		cleanText = extractSayTag(cleanText)
		cleanText = sanitizeAgentText(cleanText)

		// Fire HW markers (LED, emotion, servo, audio) on the local lamp
		// even though the spoken text goes back to the originating channel.
		h.fireHWCalls(hwCalls, runID)

		// Synthesise lifecycle_end so RESP node lights up.
		flow.Log("lifecycle_end", map[string]any{
			"run_id": runID,
			"source": "session.message",
		}, runID)
		// Clear the agent busy flag — lelamp's turn-gate hook called
		// /api/openclaw/busy when this channel turn was preprocessed, but
		// the agent-path lifecycle.end that normally clears it never fires
		// for channel turns (OpenClaw 5.x gate). Without this, sensing
		// events queue for up to busyTTL (5 min) before auto-clearing.
		h.agentGateway.SetBusy(false)

		// Channel turns: TTS stays silent on the speaker. OpenClaw 5.4 queue
		// mode does NOT auto-deliver replies to the originating Telegram chat
		// when session is `agent:main:main`, so Lumi DMs the reply via Bot API
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
			if telegramID != "" {
				// FIXME: band-aid for OpenClaw 5.4 queue-mode regression. The
				// telegram plugin closes its message-processing window in
				// ~1–2s and reports "turn ended without visible final
				// response" before the agent (which can take 20s+) finishes.
				// The eventual reply lands in chat history but never gets
				// fanned out to the originating Telegram chat. Until OpenClaw
				// fixes that path, Lumi DMs via Bot API itself. REMOVE this
				// goroutine + flow.Log when upstream fix lands — otherwise
				// users will receive duplicate replies (one from OpenClaw,
				// one from Lumi). The interleave fix above is a separate
				// case and should stay even after upstream fixes this one.
				go func(t, tid string) {
					slog.Info("channel turn → Telegram DM", "component", "agent", "run_id", runID, "telegram_id", tid)
					if err := h.agentGateway.SendToUser(tid, t, ""); err != nil {
						slog.Error("channel turn DM failed", "component", "agent", "run_id", runID, "err", err)
					}
				}(cleanText, telegramID)
				flow.Log("telegram_dm_send", map[string]any{
					"run_id":      runID,
					"telegram_id": telegramID,
					"source":      "channel_turn",
				}, runID)
			} else {
				slog.Warn("channel turn has no telegram_id — reply not delivered",
					"component", "agent", "run_id", runID, "sender_label", "elided")
			}
		}
		// Drop the channelRuns marker — turn is finished.
		h.channelRunsMu.Lock()
		delete(h.channelRuns, runID)
		h.channelRunsMu.Unlock()

	default:
		// Unhandled WS events (health, heartbeat, cron, shutdown, etc.) — no-op.
	}

	return nil
}

// extractLastUserMessageFromHistory parses a chat.history payload and returns
// the most recent role:"user" message text plus its senderLabel (empty if
// absent). Content can be a plain string or an array of {type,text} blocks;
// both shapes are handled. Returns ("","") if the payload is malformed or has
// no user messages.
func extractLastUserMessageFromHistory(payload json.RawMessage) (text string, senderLabel string) {
	var hist struct {
		Messages []struct {
			Role        string          `json:"role"`
			Content     json.RawMessage `json:"content"`
			SenderLabel string          `json:"senderLabel"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return "", ""
	}
	for i := len(hist.Messages) - 1; i >= 0; i-- {
		if hist.Messages[i].Role != "user" {
			continue
		}
		senderLabel = hist.Messages[i].SenderLabel
		var s string
		if json.Unmarshal(hist.Messages[i].Content, &s) == nil {
			return s, senderLabel
		}
		var blocks []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if json.Unmarshal(hist.Messages[i].Content, &blocks) == nil {
			var parts []string
			for _, b := range blocks {
				if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
					parts = append(parts, b.Text)
				}
			}
			return strings.Join(parts, " "), senderLabel
		}
		return "", senderLabel
	}
	return "", ""
}
