package openclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// GetConfigJSON reads and returns the raw bytes of openclaw.json.
func (s *OpenclawService) GetConfigJSON() (json.RawMessage, error) {
	path := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read openclaw.json: %w", err)
	}
	return json.RawMessage(data), nil
}

// GetConfiguredChannel reads openclaw.json and returns the first enabled channel name.
// Falls back to "channel" if none can be determined.
func (s *OpenclawService) GetConfiguredChannel() string {
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return "channel"
	}
	var cfg struct {
		Channels map[string]struct {
			Enabled *bool `json:"enabled"`
		} `json:"channels"`
	}
	if json.Unmarshal(data, &cfg) != nil {
		return "channel"
	}
	// Return the first enabled channel found. Priority order: telegram, discord, slack.
	for _, name := range []string{"telegram", "discord", "slack"} {
		if ch, ok := cfg.Channels[name]; ok {
			if ch.Enabled == nil || *ch.Enabled {
				return name
			}
		}
	}
	// Fallback: any channel present in config.
	for name, ch := range cfg.Channels {
		if ch.Enabled == nil || *ch.Enabled {
			return name
		}
	}
	return "channel"
}

// SendChatMessage sends a user message to the OpenClaw agent via WebSocket chat.send RPC.
// Returns the reqID on success.
func (s *OpenclawService) SendChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "user")
}

// SendSystemChatMessage sends a system-originated message (skill watcher notifications,
// wake greeting, /compact, …) so Flow Monitor can distinguish it from real user input.
// The WS RPC payload is identical to SendChatMessage — only the flow event `type` differs.
func (s *OpenclawService) SendSystemChatMessage(message string) (string, error) {
	return s.sendChat(message, "", "", "", "system")
}

// SendChatMessageWithImage sends a message with a base64 JPEG image to the OpenClaw agent.
// The image is included as a vision content block so the LLM can analyze the camera snapshot.
func (s *OpenclawService) SendChatMessageWithImage(message string, imageBase64 string) (string, error) {
	return s.sendChat(message, imageBase64, "", "", "user")
}

// NextChatRunID allocates ids for the next chat.send so callers can flow.SetTrace(runID) before flow.Start.
func (s *OpenclawService) NextChatRunID() (reqID string, runID string) {
	reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
	runID = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	return reqID, runID
}

// SendChatMessageWithRun sends using ids from NextChatRunID (must match that pair).
func (s *OpenclawService) SendChatMessageWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user")
}

// SendChatMessageWithImageAndRun sends with image using ids from NextChatRunID.
func (s *OpenclawService) SendChatMessageWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user")
}

// SendSlashCommandWithRun sends a slash-prefixed message (e.g. "/status") with
// deliver:false so the gateway routes the reply only back to this client and
// does not broadcast to bound channels (Telegram/Discord). Mirrors gw web's
// chat.send behavior — OpenClaw's system prompt then dispatches the slash to
// the appropriate tool (e.g. session_status). Use only when the message text
// starts with "/" and originates from the web monitor chat.
func (s *OpenclawService) SendSlashCommandWithRun(message string, reqID string, runID string) (string, error) {
	return s.sendChat(message, "", reqID, runID, "user", withDeliver(false))
}

// SendSlashCommandWithImageAndRun is SendSlashCommandWithRun with image attachment.
func (s *OpenclawService) SendSlashCommandWithImageAndRun(message string, imageBase64 string, reqID string, runID string) (string, error) {
	return s.sendChat(message, imageBase64, reqID, runID, "user", withDeliver(false))
}

// sendChatOpt is a functional option that mutates the chat.send params map
// before the payload is marshaled. New flags can be added without changing
// the sendChat signature or any existing call sites.
type sendChatOpt func(map[string]interface{})

// withDeliver sets the chat.send `deliver` flag. Pass false for slash
// commands so the gateway routes the reply only back to this caller (and
// does not broadcast to bound channels).
func withDeliver(v bool) sendChatOpt {
	return func(p map[string]interface{}) { p["deliver"] = v }
}

// sendChat is the internal implementation for sending chat messages, optionally with an image.
// If fixedReqID and fixedRunID are both non-empty, they are used (caller already incremented reqCounter via NextChatRunID).
// sourceType labels the flow event ("user" for real user / sensing-driven input, "system" for
// watcher / wake / compact notifications). Does not affect the WS RPC payload.
func (s *OpenclawService) sendChat(message string, imageBase64 string, fixedReqID string, fixedRunID string, sourceType string, opts ...sendChatOpt) (string, error) {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return "", fmt.Errorf("websocket not connected")
	}

	// reqID labels outbound chat.send from the os server (sensing POST, wake greeting, etc.) — not "audio only".
	// Idempotency key must stay stable for OpenClaw run_id mapping; use device-chat-* (not device-sensing-*)
	// so logs are not mistaken for sound/voice-only turns vs Telegram.
	var reqID string
	var idempotencyKey string
	if fixedReqID != "" && fixedRunID != "" {
		reqID = fixedReqID
		idempotencyKey = fixedRunID
	} else {
		reqID = fmt.Sprintf("chat-%d", s.reqCounter.Add(1))
		idempotencyKey = fmt.Sprintf("device-%s-%d", reqID, time.Now().UnixMilli())
	}

	params := map[string]interface{}{
		"idempotencyKey": idempotencyKey,
	}
	sessionKey := s.GetSessionKey()
	if sessionKey != "" {
		params["sessionKey"] = sessionKey
	}
	for _, opt := range opts {
		opt(params)
	}

	// Strip [snapshot: ...] paths from presence events before sending to agent —
	// face recognition already ran, agent doesn't need file paths (wastes tokens).
	// Keep original message for flow/monitor so UI can render snapshot thumbnails.
	wsMessage := message
	if strings.Contains(message, "[sensing:presence.enter]") || strings.Contains(message, "[sensing:presence.leave]") {
		wsMessage = strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	}
	params["message"] = wsMessage
	// Track the form OpenClaw will rebroadcast (post-strip) so the SSE
	// session.message handler can skip the echo and not mistake it for
	// real telegram/channel input.
	s.markOutboundChat(wsMessage)
	// Emit chat_input flow event so Flow Monitor's IN field shows the
	// actual message text. Without this, device-chat-* turns render as
	// "Input not captured" because the agent path skips chat.history
	// fetch for os-server-originated runs (only channel turns hit that path).
	previewMsg := message
	if len(previewMsg) > 500 {
		previewMsg = previewMsg[:500] + "…"
	}
	flow.Log("chat_input", map[string]any{
		"run_id":  idempotencyKey,
		"source":  sourceType,
		"message": previewMsg,
	}, idempotencyKey)
	hasImage := imageBase64 != ""
	if hasImage {
		// OpenClaw chat.send accepts attachments[]{content, mimeType} — content is raw base64 string.
		imgLen := len(imageBase64)
		params["attachments"] = []map[string]interface{}{
			{
				"type":     "image",
				"mimeType": "image/jpeg",
				"content":  imageBase64,
			},
		}
		slog.Info("[chat.send] attaching image", "component", "openclaw",
			"reqId", reqID, "runId", idempotencyKey,
			"base64Len", imgLen, "approxKB", imgLen*3/4/1024)
	}

	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "chat.send",
		"params": params,
	}
	body, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal chat.send: %w", err)
	}

	// Log full payload (mask image content to avoid log spam)
	slog.Info("[chat.send] full payload", "component", "openclaw", "reqId", reqID, "payload", string(body))
	slog.Info("[chat.send] >>> sending to OpenClaw", "component", "openclaw",
		"reqId", reqID, "runId", idempotencyKey,
		"sessionKey", sessionKey,
		"message", message,
		"hasImage", hasImage,
		"attachments", func() string {
			if !hasImage {
				return "none"
			}
			return fmt.Sprintf("1x image/jpeg ~%dKB", len(imageBase64)*3/4/1024)
		}(),
		"payloadBytes", len(body))

	s.wsMu.Lock()
	conn = s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		return "", fmt.Errorf("websocket disconnected before send")
	}
	// Set busy before write — closes the timing gap where sensing IsBusy()=false
	// because lifecycle_start SSE hasn't arrived yet. SSE lifecycle_end still clears it.
	s.busySince.Store(time.Now().UnixMilli())
	s.activeTurn.Store(true)
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		s.activeTurn.Store(false) // write failed — no turn will start, clear immediately
		slog.Error("[chat.send] write failed", "component", "openclaw",
			"reqId", reqID, "runId", idempotencyKey, "error", err)
		return "", fmt.Errorf("write chat.send: %w", err)
	}

	slog.Info("[chat.send] <<< sent OK", "component", "openclaw",
		"reqId", reqID, "runId", idempotencyKey, "hasImage", hasImage)
	// Store pending trace + exact message text so the SSE handler can map a
	// UUID lifecycle (drained from OpenClaw's followup queue, which strips
	// the idempotencyKey) back to this device runId via chat.history →
	// MatchPendingByMessage. Stores `message` (not the raw WS body) because
	// chat.history returns the user message content, not the wrapper.
	s.SetPendingChatTrace(idempotencyKey, message)
	flow.Log("chat_send", map[string]any{
		"run_id":      idempotencyKey,
		"type":        sourceType,
		"has_session": sessionKey != "",
		"has_image":   hasImage,
		"image_bytes": len(imageBase64),
		"message":     message,
	}, idempotencyKey)
	slog.Info("flow correlation", "op", "ws_chat_send", "section", "os_to_openclaw_ws",
		"device_run_id", idempotencyKey, "req_id", reqID, "has_image", hasImage)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_send",
		Summary: message,
		RunID:   idempotencyKey,
	})

	// Return idempotencyKey (not reqID) so trace_id matches OpenClaw's run_id.
	return idempotencyKey, nil
}

// CompactSession sends a sessions.compact RPC to reduce conversation history.
func (s *OpenclawService) CompactSession(sessionKey string) error {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return fmt.Errorf("ws not connected")
	}

	reqID := fmt.Sprintf("compact-%d", s.reqCounter.Add(1))
	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "sessions.compact",
		"params": map[string]interface{}{
			"key": sessionKey,
		},
	}
	body, err := json.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal compact request: %w", err)
	}

	s.wsMu.Lock()
	conn = s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return fmt.Errorf("ws not connected")
	}

	if err := conn.WriteMessage(websocket.TextMessage, body); err != nil {
		return fmt.Errorf("write compact request: %w", err)
	}

	slog.Info("sessions.compact sent", "component", "openclaw", "sessionKey", sessionKey)
	return nil
}

// NewSession resets the agent's in-session conversation history by
// sending the OpenClaw `/new` text command (alias of `/reset`) via the
// normal chat.send path. Unlike CompactSession this does not run an
// LLM summarize step — the runtime drops history and starts clean.
//
// History: an earlier version used a dedicated `sessions.new` RPC, but
// OpenClaw 5.7+ removed that method (returns INVALID_REQUEST
// `unknown method: sessions.new`). The `/new` command is the supported
// surface for this operation and is handled by OpenClaw's command
// dispatcher before any LLM call, so it does not consume a turn.
// `sessionKey` is accepted for call-site compatibility but is implicit
// in the chat.send routing — the command applies to the session keyed
// by the WS connection's active sessionKey.
func (s *OpenclawService) NewSession(sessionKey string) error {
	if _, err := s.sendChat("/new", "", "", "", "system"); err != nil {
		return fmt.Errorf("send /new: %w", err)
	}
	slog.Info("/new sent", "component", "openclaw", "sessionKey", sessionKey)
	return nil
}
