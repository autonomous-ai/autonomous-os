package hermes

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// SendChatMessage sends a user message to Hermes via POST /v1/responses.
// Returns the run ID (== idempotency key) the caller should use to correlate
// flow/monitor events with the resulting SSE stream.
func (s *Service) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "user", nil)
}

// SendSystemChatMessage flags the flow event as a system-originated message
// (skill watcher, wake greeting, /compact) so Flow Monitor renders it
// separately from real user input. Wire payload is identical otherwise.
func (s *Service) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "system", nil)
}

func (s *Service) SendChatMessageWithImage(message string, imageBase64 string) (string, error) {
	return s.sendChat(message, imageBase64, "", "", "user", nil)
}

// NextChatRunID allocates the run / req id pair. Caller flow.SetTrace(runID)
// before flow.Start so the sensing_input enter line matches the eventual
// chat_send. Same shape as openclaw's allocator so logs / monitor stay
// identical across backends.
func (s *Service) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *Service) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user", nil)
}

func (s *Service) SendChatMessageWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user", nil)
}

// SendSlashCommandWithRun — Hermes has no per-channel "deliver:false" flag,
// so slash commands look the same as any other user input on the wire.
// Marker: we still tag the flow source so logs distinguish "this came from
// web monitor" vs voice.
func (s *Service) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user_slash", nil)
}

func (s *Service) SendSlashCommandWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user_slash", nil)
}

// sendChat is the internal entry. It:
//   1. allocates ids if not provided,
//   2. marks busy + records pending trace,
//   3. emits chat_input / chat_send flow events for parity with openclaw,
//   4. builds the streamRequest (string input for text-only, array w/ image),
//   5. fires postStream in a background goroutine and dispatches translated
//      events into the registered handler.
//
// Returns the device run ID (idempotency-style) once the POST has been
// kicked off — not after response.completed. Caller correlates via SSE.
func (s *Service) sendChat(message string, imageBase64 string, fixedReqID string, fixedRunID string, sourceType string, _ any) (string, error) {
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
		Conversation: Conversation,
		Stream:       true,
	}
	hasImage := imageBase64 != ""
	if hasImage {
		body.Input = []inputMessage{{
			Role: "user",
			Content: []inputContent{
				{Type: "input_text", Text: wsMessage},
				{Type: "input_image", ImageURL: "data:image/jpeg;base64," + imageBase64},
			},
		}}
		slog.Info("[hermes /v1/responses] attaching image", "component", "hermes",
			"reqId", reqID, "runId", idempotencyKey,
			"base64Len", len(imageBase64), "approxKB", len(imageBase64)*3/4/1024)
	} else {
		body.Input = wsMessage
	}

	// Mark busy before the network round-trip so sensing-while-busy gates
	// catch the in-flight turn even before response.created arrives. Cleared
	// by the lifecycle.end translator (or by SetBusy(false) on error).
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)

	s.SetPendingChatTrace(idempotencyKey, message)

	slog.Info("hermes >>> SEND  user message", "component", "hermes",
		"reqId", reqID,
		"runId", idempotencyKey,
		"sessionKey", s.GetSessionKey(),
		"conversation", body.Conversation,
		"model", body.Model,
		"source", sourceType,
		"hasImage", hasImage,
		"imageBytes", len(imageBase64),
		"msgLen", len(message),
		"message", truncRunes(message, 500))

	flow.Log("chat_send", map[string]any{
		"run_id":      idempotencyKey,
		"type":        sourceType,
		"has_session": s.GetSessionKey() != "",
		"has_image":   hasImage,
		"image_bytes": len(imageBase64),
		"message":     message,
	}, idempotencyKey)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_send",
		Summary: message,
		RunID:   idempotencyKey,
	})

	// Run the SSE stream in a background goroutine: Lumi callers (sensing
	// handler, voice loop) shouldn't block for the full turn duration.
	go s.runStream(idempotencyKey, body)

	return idempotencyKey, nil
}

// runStream issues the POST and pumps translated events into the registered
// handler. Runs in its own goroutine — one per outbound chat.send.
func (s *Service) runStream(runID string, body streamRequest) {
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

func (s *Service) currentHandler() domain.AgentEventHandler {
	s.handlerMu.Lock()
	h := s.handler
	s.handlerMu.Unlock()
	return h
}
