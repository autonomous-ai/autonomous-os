package openclaw

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

// StartWS connects to the gateway WebSocket and runs the read loop, calling handler for each event.
// It runs until ctx is cancelled. Auto-reconnects when disconnected.
func (s *OpenclawService) StartWS(ctx context.Context, handler domain.AgentEventHandler) {
	backoff := 5 * time.Second
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		err := s.runWSConn(ctx, handler)
		if ctx.Err() != nil {
			return
		}
		// Skip the cyan status overlay during AP/provisioning mode: the WS
		// is doomed to fail until the user finishes setup (no openclaw
		// credentials yet), and StateAgentDown would clobber the
		// setup-needed solid white painted by server.waitAndPaintSetupReady.
		if s.statusLED != nil && s.config.SetUpCompleted {
			s.statusLED.Set(statusled.StateAgentDown)
		}
		// Safety reflex: the gateway link just dropped, so any in-flight servo
		// object-tracking is now chasing a target it can no longer get vision
		// updates for. Stop it (best-effort, idempotent) so the body doesn't
		// keep aiming at stale coordinates. Local idle animation is untouched —
		// it's harmless and self-contained — and recovery reflexes stay
		// available. Never block or affect the reconnect/backoff loop below.
		// Only devices that can servo-track (motion) have anything to stop —
		// skip on bodies without it (e.g. intern-v2) so we don't POST to a
		// /servo endpoint they don't serve.
		if s.config.SetUpCompleted && device.Has(s.config.DeviceTypeOrDefault(), device.CapMotion) {
			if err := hal.StopServoTracking(); err != nil {
				slog.Warn("stop servo tracking on ws disconnect failed", "component", "openclaw", "error", err)
			}
		}
		if err != nil {
			slog.Warn("websocket disconnected, reconnecting", "component", "openclaw", "error", err, "backoff", backoff)
			flow.Log("ws_disconnect", map[string]any{"error": err.Error(), "backoff_s": backoff.Seconds()})
		} else {
			slog.Warn("websocket connection closed, reconnecting", "component", "openclaw", "backoff", backoff)
			flow.Log("ws_disconnect", map[string]any{"reason": "closed", "backoff_s": backoff.Seconds()})
		}
		if !sleepCtx(ctx, backoff) {
			return
		}
	}
}

func (s *OpenclawService) runWSConn(ctx context.Context, handler domain.AgentEventHandler) error {
	s.wsConnected.Store(false)
	s.wsConnectedAt.Store(0)
	defer func() {
		s.wsConnected.Store(false)
		s.wsConnectedAt.Store(0)
	}()
	defer s.activeTurn.Store(false) // clear busy on disconnect — lifecycle_end may never arrive

	connStart := flow.Start("ws_connect", map[string]any{"url": defaultGatewayWSURL})

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, resp, err := dialer.DialContext(ctx, defaultGatewayWSURL, http.Header{})
	if err != nil {
		if resp != nil {
			flow.End("ws_connect", connStart, map[string]any{"error": err.Error(), "status": resp.Status})
			return fmt.Errorf("dial %s: %w (status %s)", defaultGatewayWSURL, err, resp.Status)
		}
		flow.End("ws_connect", connStart, map[string]any{"error": err.Error()})
		return fmt.Errorf("dial %s: %w", defaultGatewayWSURL, err)
	}
	defer func() {
		s.wsMu.Lock()
		s.wsConn = nil
		s.wsMu.Unlock()
		conn.Close()
	}()

	// Read connect.challenge from gateway
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, msg, err := conn.ReadMessage()
	if err != nil {
		return fmt.Errorf("read connect.challenge: %w", err)
	}
	conn.SetReadDeadline(time.Time{})
	slog.Debug("initial event received", "component", "openclaw", "event", string(msg))

	// Parse nonce from connect.challenge
	var challenge struct {
		Payload struct {
			Nonce string `json:"nonce"`
		} `json:"payload"`
	}
	if err := json.Unmarshal(msg, &challenge); err != nil || challenge.Payload.Nonce == "" {
		return fmt.Errorf("parse connect.challenge nonce: %w", err)
	}
	nonce := challenge.Payload.Nonce

	token, err := s.readGatewayToken()
	if err != nil {
		flow.End("ws_connect", connStart, map[string]any{"error": "read token: " + err.Error()})
		return fmt.Errorf("read gateway token: %w", err)
	}

	di, err := s.loadOrCreateDeviceIdentity()
	if err != nil {
		return fmt.Errorf("device identity: %w", err)
	}

	signedAt := time.Now().UnixMilli()
	signature := di.signConnectPayload(token, nonce, signedAt)

	connectReq := map[string]interface{}{
		"type":   "req",
		"id":     "os-1",
		"method": "connect",
		"params": map[string]interface{}{
			"minProtocol": 3,
			"maxProtocol": 4,
			"client": map[string]interface{}{
				"id":       "node-host",
				"version":  "1.0",
				"platform": "linux",
				"mode":     "node",
			},
			"role":   "operator",
			"scopes": []string{"operator.read", "operator.write", "operator.admin"},
			"caps":   []string{"thinking-events", "tool-events"},
			"auth":   map[string]interface{}{"token": token},
			"device": map[string]interface{}{
				"id":        di.DeviceID,
				"publicKey": base64.StdEncoding.EncodeToString(di.PublicKey),
				"signature": signature,
				"signedAt":  signedAt,
				"nonce":     nonce,
			},
		},
	}
	connectBody, _ := json.Marshal(connectReq)
	if err := conn.WriteMessage(websocket.TextMessage, connectBody); err != nil {
		return fmt.Errorf("write connect: %w", err)
	}

	// Read connect response — extract sessionKey if present
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, connectResp, err := conn.ReadMessage()
	if err != nil {
		return fmt.Errorf("read connect response: %w", err)
	}
	conn.SetReadDeadline(time.Time{})
	slog.Debug("connect response", "component", "openclaw", "response", string(connectResp))

	// Check for pairing errors — if scope-upgrade, reset device identity and retry
	var connectErr struct {
		OK    bool `json:"ok"`
		Error struct {
			Code    string `json:"code"`
			Details struct {
				Reason string `json:"reason"`
			} `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(connectResp, &connectErr) == nil && !connectErr.OK && connectErr.Error.Code == "NOT_PAIRED" {
		reason := connectErr.Error.Details.Reason
		if reason == "scope-upgrade" || reason == "not-paired" {
			slog.Warn("pairing rejected, resetting device identity to re-pair",
				"component", "openclaw", "reason", reason)
			keyPath := filepath.Join(s.config.OpenclawConfigDir, deviceKeyFile)
			if err := os.Remove(keyPath); err != nil && !os.IsNotExist(err) {
				slog.Error("failed to remove device key", "component", "openclaw", "error", err)
			}
			return fmt.Errorf("pairing rejected (%s): identity reset, will re-pair on next connect", reason)
		}
		return fmt.Errorf("pairing rejected: %s", connectErr.Error.Code)
	}

	var connectResult struct {
		Type   string `json:"type"`
		Result struct {
			SessionKey string `json:"sessionKey"`
		} `json:"result"`
		Payload struct {
			// uptimeMs may live at payload top level (older gateway responses) or
			// inside snapshot (current 2026.5.27 hello-ok shape) — capture both,
			// prefer the non-zero one.
			UptimeMs int64 `json:"uptimeMs"`
			Snapshot struct {
				UptimeMs        int64 `json:"uptimeMs"`
				SessionDefaults struct {
					MainSessionKey string `json:"mainSessionKey"`
				} `json:"sessionDefaults"`
			} `json:"snapshot"`
		} `json:"payload"`
	}
	if err := json.Unmarshal(connectResp, &connectResult); err == nil {
		sk := connectResult.Result.SessionKey
		if sk == "" {
			sk = connectResult.Payload.Snapshot.SessionDefaults.MainSessionKey
		}
		if sk != "" {
			s.SetSessionKey(sk)
			slog.Info("session key from connect", "component", "openclaw", "sessionKey", sk)
		}
		// Compute the OpenClaw process start time so the monitor UI can show the
		// gateway's true age, not Lamp's WS connection age. Try both known
		// locations of uptimeMs in the hello-ok payload.
		upMs := connectResult.Payload.UptimeMs
		if upMs <= 0 {
			upMs = connectResult.Payload.Snapshot.UptimeMs
		}
		if upMs > 0 {
			s.agentStartedAt.Store(time.Now().Unix() - upMs/1000)
		}
	}

	// If no session key yet, request sessions.list to find an active session
	if s.GetSessionKey() == "" {
		listReq := map[string]interface{}{
			"type":   "req",
			"id":     "os-sessions",
			"method": "sessions.list",
		}
		listBody, _ := json.Marshal(listReq)
		if err := conn.WriteMessage(websocket.TextMessage, listBody); err == nil {
			conn.SetReadDeadline(time.Now().Add(5 * time.Second))
			_, listResp, err := conn.ReadMessage()
			conn.SetReadDeadline(time.Time{})
			if err == nil {
				slog.Debug("sessions.list response", "component", "openclaw", "response", string(listResp))
				var listResult struct {
					Result struct {
						Sessions []struct {
							SessionKey string `json:"sessionKey"`
						} `json:"sessions"`
					} `json:"result"`
				}
				if json.Unmarshal(listResp, &listResult) == nil && len(listResult.Result.Sessions) > 0 {
					sk := listResult.Result.Sessions[0].SessionKey
					s.SetSessionKey(sk)
					slog.Info("session key from sessions.list", "component", "openclaw", "sessionKey", sk)
				}
			}
		}
	}

	s.wsMu.Lock()
	s.wsConn = conn
	s.wsMu.Unlock()
	s.wsConnected.Store(true)
	s.wsConnectedAt.Store(time.Now().Unix())
	if s.statusLED != nil && s.config.SetUpCompleted {
		s.statusLED.Clear(statusled.StateAgentDown)
	}
	flow.End("ws_connect", connStart, map[string]any{"session_key": s.GetSessionKey() != ""})
	flow.Log("ws_ready", map[string]any{"session": s.GetSessionKey() != ""})

	// On reconnect (not first boot), announce via TTS so user knows agent is back.
	// hal.Speak (not SendToHALTTS): this is a hardcoded system filler, so it
	// must NOT be fed to the realtime voice agent as history.
	if s.wsHasConnected.Swap(true) {
		go func() {
			phrase := i18n.Pick(i18n.PhraseReconnect)
			if err := hal.Speak(phrase); err != nil {
				slog.Warn("reconnect TTS failed", "component", "openclaw", "error", err)
			}
		}()
	}

	// Subscribe to session events so we receive tool events for all turns
	// (including Telegram-initiated turns where Lamp didn't call chat.send).
	subReq := map[string]interface{}{
		"type":   "req",
		"id":     fmt.Sprintf("sub-%d", s.reqCounter.Add(1)),
		"method": "sessions.subscribe",
		"params": map[string]interface{}{},
	}
	if body, err := json.Marshal(subReq); err == nil {
		s.wsMu.Lock()
		_ = conn.WriteMessage(websocket.TextMessage, body)
		s.wsMu.Unlock()
		slog.Info("sessions.subscribe sent", "component", "openclaw")
	}

	// Event handler runs in a worker goroutine so it does not block the read
	// loop. This matters because the handler may call FetchChatHistory (a WS
	// RPC) — if the read loop is stuck inside the handler waiting for the RPC
	// response, the response frame never gets read, and the call deadlocks.
	// With the worker model, the read loop keeps consuming frames and routes
	// the response to dispatchRPCResponse independently, unblocking the
	// pending RPC inside the handler.
	//
	// The channel is buffered to absorb short bursts; if the handler stalls
	// past the buffer, the read loop blocks on send (back-pressure) instead
	// of dropping events — order is preserved end-to-end.
	const eventChanBuf = 64
	eventCh := make(chan domain.WSEvent, eventChanBuf)
	handlerDone := make(chan struct{})
	go func() {
		defer close(handlerDone)
		defer func() {
			if r := recover(); r != nil {
				slog.Error("ws handler panic — worker exiting", "component", "openclaw", "panic", r)
			}
		}()
		for evt := range eventCh {
			if handler == nil {
				continue
			}
			if err := handler(ctx, evt); err != nil {
				slog.Error("ws handler error", "component", "openclaw", "event", evt.Event, "error", err)
				// Do not exit on handler error — keep processing subsequent
				// events. Read loop exit is driven by socket error or ctx.
			}
		}
	}()
	// Stop the worker when this read loop returns (socket error, ctx done,
	// or reconnect path). Drain ensures the next iteration of startWS gets a
	// fresh worker.
	defer func() {
		close(eventCh)
		<-handlerDone
	}()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		// Try to extract sessionKey from any message (fallback if connect response didn't have it)
		if s.GetSessionKey() == "" {
			var raw struct {
				SessionKey string `json:"sessionKey"`
				Result     struct {
					SessionKey string `json:"sessionKey"`
				} `json:"result"`
				Payload json.RawMessage `json:"payload"`
			}
			if json.Unmarshal(msg, &raw) == nil {
				sk := raw.SessionKey
				if sk == "" {
					sk = raw.Result.SessionKey
				}
				if sk == "" && len(raw.Payload) > 0 {
					var p struct {
						SessionKey string `json:"sessionKey"`
					}
					if json.Unmarshal(raw.Payload, &p) == nil {
						sk = p.SessionKey
					}
				}
				if sk != "" {
					s.SetSessionKey(sk)
				}
			}
		}

		// Dispatch RPC responses to pending callers immediately in the read
		// loop. This must happen before pushing to eventCh: chat.history and
		// other RPCs called from inside the handler block on pendingRPC
		// channels, which can only be delivered here.
		s.dispatchRPCResponse(msg)

		var evt domain.WSEvent
		if err := json.Unmarshal(msg, &evt); err != nil {
			continue
		}
		// Push to worker. Blocks only if buffer is full (handler is far
		// behind) — back-pressure is preferable to dropping events because
		// it preserves order and surfaces handler slowdowns as latency.
		select {
		case eventCh <- evt:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// dispatchRPCResponse checks if msg is an RPC response and delivers it to the waiting caller.
func (s *OpenclawService) dispatchRPCResponse(msg []byte) {
	var frame struct {
		Type    string          `json:"type"`
		ID      string          `json:"id"`
		OK      bool            `json:"ok"`
		Payload json.RawMessage `json:"payload"`
	}
	if json.Unmarshal(msg, &frame) != nil || frame.Type != "res" || frame.ID == "" {
		return
	}
	s.pendingRPCMu.Lock()
	ch, ok := s.pendingRPC[frame.ID]
	if ok {
		delete(s.pendingRPC, frame.ID)
	}
	s.pendingRPCMu.Unlock()
	if ok {
		select {
		case ch <- frame.Payload:
		default:
		}
	}
}

// FetchChatHistory sends a chat.history RPC and returns the raw payload.
// Best-effort with a 3-second timeout; returns nil on any failure.
func (s *OpenclawService) FetchChatHistory(sessionKey string, limit int) (json.RawMessage, error) {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return nil, fmt.Errorf("websocket not connected")
	}
	if sessionKey == "" {
		sessionKey = s.GetSessionKey()
	}
	if sessionKey == "" {
		return nil, fmt.Errorf("no session key")
	}

	reqID := fmt.Sprintf("history-%d", s.reqCounter.Add(1))
	ch := make(chan json.RawMessage, 1)

	s.pendingRPCMu.Lock()
	s.pendingRPC[reqID] = ch
	s.pendingRPCMu.Unlock()

	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "chat.history",
		"params": map[string]interface{}{
			"sessionKey": sessionKey,
			"limit":      limit,
		},
	}
	body, err := json.Marshal(req)
	if err != nil {
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("marshal chat.history: %w", err)
	}

	s.wsMu.Lock()
	conn = s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("websocket disconnected before send")
	}
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("write chat.history: %w", err)
	}

	timer := time.NewTimer(3 * time.Second)
	defer timer.Stop()
	select {
	case payload := <-ch:
		return payload, nil
	case <-timer.C:
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("chat.history timeout")
	}
}

func (s *OpenclawService) readGatewayToken() (string, error) {
	path := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	var cfg struct {
		Gateway struct {
			Auth struct {
				Token string `json:"token"`
			} `json:"auth"`
		} `json:"gateway"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return "", err
	}
	token := cfg.Gateway.Auth.Token
	if token == "" {
		return "", fmt.Errorf("gateway.auth.token is empty in %s", path)
	}
	return token, nil
}
