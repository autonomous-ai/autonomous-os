package hermes

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// SendChatMessage sends a user message to Hermes via POST /v1/responses.
// Returns the run ID (== idempotency key) the caller should use to correlate
// flow/monitor events with the resulting SSE stream.
func (s *HermesService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "user", nil)
}

// SendSystemChatMessage flags the flow event as a system-originated message
// (skill watcher, wake greeting, /compact) so Flow Monitor renders it
// separately from real user input. Wire payload is identical otherwise.
func (s *HermesService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, nil, "", "", "system", nil)
}

func (s *HermesService) SendChatMessageWithImages(message string, imagesBase64 []string) (string, error) {
	return s.sendChat(message, imagesBase64, "", "", "user", nil)
}

// NextChatRunID allocates the run / req id pair. Caller flow.SetTrace(runID)
// before flow.Start so the sensing_input enter line matches the eventual
// chat_send. Same shape as openclaw's allocator so logs / monitor stay
// identical across backends.
func (s *HermesService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *HermesService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user", nil)
}

func (s *HermesService) SendChatMessageWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user", nil)
}

// SendSlashCommandWithRun — Hermes has no per-channel "deliver:false" flag,
// so slash commands look the same as any other user input on the wire.
// Marker: we still tag the flow source so logs distinguish "this came from
// web monitor" vs voice.
func (s *HermesService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, nil, reqID, runID, "user_slash", nil)
}

func (s *HermesService) SendSlashCommandWithImagesAndRun(message string, imagesBase64 []string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imagesBase64, reqID, runID, "user_slash", nil)
}

// sendChat is the internal entry. It:
//  1. allocates ids if not provided,
//  2. marks busy + records pending trace,
//  3. emits chat_input / chat_send flow events for parity with openclaw,
//  4. builds the streamRequest (string input for text-only, array w/ image),
//  5. fires postStream in a background goroutine and dispatches translated
//     events into the registered handler.
//
// Returns the device run ID (idempotency-style) once the POST has been
// kicked off — not after response.completed. Caller correlates via SSE.
func (s *HermesService) sendChat(message string, imagesBase64 []string, fixedReqID string, fixedRunID string, sourceType string, _ any) (string, error) {
	if !s.ready.Load() {
		return "", fmt.Errorf("hermes not ready")
	}

	var reqID, idempotencyKey string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		idempotencyKey = fixedRunID
	} else {
		reqID, idempotencyKey = s.NextChatRunID()
	}

	// Strip [snapshot: ...] paths from presence events so the agent doesn't
	// waste tokens on file paths it has no tools to access. Matches the
	// openclaw codepath at service_chat.go.
	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
	s.markOutboundChat(wsMessage)

	previewMsg := message
	if len(previewMsg) > 500 {
		previewMsg = previewMsg[:500] + "…"
	}
	flow.Log("chat_input", map[string]any{
		"run_id":  idempotencyKey,
		"source":  sourceType,
		"message": previewMsg,
	}, idempotencyKey)

	body := streamRequest{
		Model:        Model,
		Conversation: s.conversationName(),
		Stream:       true,
	}
	// One input_image block per attached photo, after the single text block —
	// the content list is what lets a turn carry several images at once.
	content := []inputContent{{Type: "input_text", Text: wsMessage}}
	imgLen := 0
	for _, img := range imagesBase64 {
		if img == "" {
			continue
		}
		imgLen += len(img)
		content = append(content, inputContent{Type: "input_image", ImageURL: "data:image/jpeg;base64," + img})
	}
	hasImage := len(content) > 1
	if hasImage {
		body.Input = []inputMessage{{Role: "user", Content: content}}
		slog.Info("[hermes /v1/responses] attaching images", "component", "hermes",
			"reqId", reqID, "runId", idempotencyKey, "count", len(content)-1,
			"base64Len", imgLen, "approxKB", imgLen*3/4/1024)
	} else {
		body.Input = wsMessage
	}

	// Mark busy before the network round-trip so sensing-while-busy gates
	// catch the in-flight turn even before response.created arrives. Cleared
	// by the lifecycle.end translator (or by SetBusy(false) on error).
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)

	// Flash the "thinking" face for visible turns (OpenClaw emotion-acknowledge
	// hook parity). Skips passive sensing + realtime-handled turns. See emotion_ack.go.
	s.fireAckEmotion(idempotencyKey, message)

	s.SetPendingChatTrace(idempotencyKey, message)

	slog.Info("hermes >>> SEND  user message", "component", "hermes",
		"reqId", reqID,
		"runId", idempotencyKey,
		"sessionKey", s.GetSessionKey(),
		"conversation", body.Conversation,
		"model", body.Model,
		"source", sourceType,
		"hasImage", hasImage,
		"imageCount", len(content) - 1,
		"imageBytes", imgLen,
		"msgLen", len(message),
		"message", truncRunes(message, 500))

	flow.Log("chat_send", map[string]any{
		"run_id":      idempotencyKey,
		"type":        sourceType,
		"has_session": s.GetSessionKey() != "",
		"has_image":   hasImage,
		"image_count": len(content) - 1,
		"image_bytes": imgLen,
		"message":     message,
	}, idempotencyKey)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_send",
		Summary: message,
		RunID:   idempotencyKey,
	})

	// Run the SSE stream in a background goroutine: Device callers (sensing
	// handler, voice loop) shouldn't block for the full turn duration.
	go s.runStream(idempotencyKey, body)

	return idempotencyKey, nil
}

// runStream issues the POST and pumps translated events into the registered
// handler. Runs in its own goroutine — one per outbound chat.send.
func (s *HermesService) runStream(runID string, body streamRequest) {
	handler := s.currentHandler()
	dispatch := func(evt domain.WSEvent) {
		if handler == nil {
			return
		}
		// Best-effort: drop handler errors but keep streaming. Matches
		// the openclaw worker's "do not exit on handler error" policy.
		if err := handler(context.Background(), evt); err != nil {
			slog.Error("hermes dispatch handler error", "component", "hermes",
				"event", evt.Event, "runID", runID, "error", err)
		}
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	res, err := s.postStream(ctx, runID, body, dispatch)
	if err != nil {
		slog.Error("hermes stream error", "component", "hermes", "runID", runID, "error", err)
		// Make sure busy clears so the next sensing/voice round can proceed.
		s.activeTurn.Store(false)
		// Synthesize a lifecycle.error so flow/monitor consumers see the turn fail.
		payload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "lifecycle",
			"data": map[string]any{
				"phase":   "error",
				"error":   err.Error(),
				"endedAt": nowUnixMs(),
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
		return
	}

	if res.Errored {
		slog.Warn("hermes <<< turn FAILED", "component", "hermes",
			"runID", runID, "responseID", res.ResponseID, "error", res.ErrorText)
	} else {
		slog.Info("hermes <<< turn COMPLETE", "component", "hermes",
			"runID", runID,
			"responseID", res.ResponseID,
			"sessionID", s.GetSessionKey(),
			"finalLen", len(res.FinalText),
			"finalPreview", truncRunes(res.FinalText, 300))
	}
}

func (s *HermesService) currentHandler() domain.AgentEventHandler {
	s.handlerMu.Lock()
	h := s.handler
	s.handlerMu.Unlock()
	return h
}
