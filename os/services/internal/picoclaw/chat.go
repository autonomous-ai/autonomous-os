package picoclaw

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// SendChatMessage sends a user message to PicoClaw. Returns the run ID the
// caller uses to correlate flow/monitor events with the resulting turn.
func (s *Service) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "user")
}

// SendSystemChatMessage flags the flow event as system-originated (skill watcher,
// wake greeting, /compact). Wire payload is identical to SendChatMessage.
func (s *Service) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "system")
}

func (s *Service) SendChatMessageWithImage(message string, imageBase64 string) (string, error) {
	return s.sendChat(message, imageBase64, "", "", "user")
}

// NextChatRunID allocates the run / req id pair. Same shape as openclaw/hermes so
// logs / monitor stay identical across backends.
func (s *Service) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

func (s *Service) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user")
}

func (s *Service) SendChatMessageWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user")
}

// SendSlashCommandWithRun — PicoClaw has no per-channel "deliver:false" flag, so
// slash commands look the same as any other user input on the wire. We still tag
// the flow source so logs distinguish web-monitor input from voice.
func (s *Service) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user_slash")
}

func (s *Service) SendSlashCommandWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user_slash")
}

// sendChat allocates ids, marks busy, records the pending trace + runID, emits
// chat_input / chat_send flow events for parity with openclaw, and writes the
// message.send frame to the persistent WebSocket. The reply arrives on the read
// loop and is translated there — this returns as soon as the frame is sent.
func (s *Service) sendChat(message, imageBase64, fixedReqID, fixedRunID, sourceType string) (string, error) {
	if !s.wsConnected.Load() {
		return "", fmt.Errorf("picoclaw not connected")
	}

	var reqID, runID string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		runID = fixedRunID
	} else {
		reqID, runID = s.NextChatRunID()
	}

	// Strip [snapshot: ...] paths from presence events so the agent doesn't waste
	// tokens on file paths it has no tools to access (matches openclaw/hermes).
	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
	s.markOutboundChat(wsMessage)

	previewMsg := truncRunes(message, 500)
	flow.Log("chat_input", map[string]any{
		"run_id":  runID,
		"source":  sourceType,
		"message": previewMsg,
	}, runID)

	// Build the outbound frame. Image attachments are best-effort: the text
	// content is always sent so the turn proceeds even if PicoClaw ignores the
	// attachment shape.
	payload := map[string]any{"content": wsMessage}
	hasImage := imageBase64 != ""
	if hasImage {
		payload["attachments"] = []map[string]any{{
			"type": "image",
			"url":  "data:image/jpeg;base64," + imageBase64,
		}}
	}
	frame := map[string]any{
		"type":    "message.send",
		"id":      reqID,
		"payload": payload,
	}
	if sk := s.GetSessionKey(); sk != "" {
		frame["session_id"] = sk
	}

	// Mark busy + stash the runID BEFORE the write so the first inbound frame of
	// this turn adopts it (ensureTurnStarted) and sensing-while-busy gates catch
	// the in-flight turn. Cleared by emitFinal/handleError (or busyTTL).
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)
	s.setPendingRunID(runID)
	s.SetPendingChatTrace(runID, message)

	slog.Info("picoclaw >>> SEND user message", "component", "picoclaw",
		"reqId", reqID, "runId", runID, "sessionKey", s.GetSessionKey(),
		"source", sourceType, "hasImage", hasImage, "msgLen", len(message),
		"message", truncRunes(message, 500))

	flow.Log("chat_send", map[string]any{
		"run_id":      runID,
		"type":        sourceType,
		"has_session": s.GetSessionKey() != "",
		"has_image":   hasImage,
		"message":     message,
	}, runID)

	s.monitorBus.Push(domain.MonitorEvent{Type: "chat_send", Summary: message, RunID: runID})

	if err := s.sendFrame(frame); err != nil {
		// Roll back busy so the next sensing/voice round can proceed.
		s.activeTurn.Store(false)
		s.clearTurn()
		slog.Error("picoclaw send failed", "component", "picoclaw", "runID", runID, "error", err)
		return "", fmt.Errorf("send message.send: %w", err)
	}

	return runID, nil
}
