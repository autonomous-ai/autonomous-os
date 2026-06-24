package picoclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
)

const (
	// reconnectBackoff is the fixed wait between reconnect attempts. PicoClaw is
	// local so a short, constant backoff is fine (matches openclaw).
	reconnectBackoff = 5 * time.Second
	// readDeadline bounds how long the read loop blocks waiting for a frame. It
	// is refreshed on every inbound frame (including pong) so a healthy-but-idle
	// socket is kept alive by the keepalive ping below.
	readDeadline = 90 * time.Second
	// pingInterval is how often we send an application-level ping so the server
	// keeps the connection warm and our readDeadline keeps getting fed.
	pingInterval = 25 * time.Second
)

// StartWS connects to the PicoClaw WebSocket and runs the read loop, calling
// handler for each translated event. Runs until ctx is cancelled, auto-
// reconnecting on drop. Mirrors the openclaw.PicoclawService.StartWS shape.
func (s *PicoclawService) StartWS(ctx context.Context, handler domain.AgentEventHandler) {
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
		// Skip the cyan status overlay during AP/provisioning mode (no creds yet).
		if s.statusLED != nil && s.config.SetUpCompleted {
			s.statusLED.Set(statusled.StateAgentDown)
		}
		// Safety reflex: the gateway link just dropped, so any in-flight servo
		// object-tracking is now chasing a target it can no longer get vision
		// updates for. Stop it (best-effort, idempotent). Only devices that can
		// servo-track have anything to stop.
		if s.config.SetUpCompleted && device.Has(s.config.DeviceTypeOrDefault(), device.CapMotion) {
			if err := hal.StopServoTracking(); err != nil {
				slog.Warn("stop servo tracking on ws disconnect failed", "component", "picoclaw", "error", err)
			}
		}
		if err != nil {
			slog.Warn("websocket disconnected, reconnecting", "component", "picoclaw", "error", err, "backoff", reconnectBackoff)
			flow.Log("ws_disconnect", map[string]any{"error": err.Error(), "backoff_s": reconnectBackoff.Seconds()})
		} else {
			slog.Warn("websocket connection closed, reconnecting", "component", "picoclaw", "backoff", reconnectBackoff)
			flow.Log("ws_disconnect", map[string]any{"reason": "closed", "backoff_s": reconnectBackoff.Seconds()})
		}
		if !sleepCtx(ctx, reconnectBackoff) {
			return
		}
	}
}

// runWSConn dials, marks the socket ready, then pumps inbound frames through the
// translator until the socket errors or ctx is cancelled.
func (s *PicoclawService) runWSConn(ctx context.Context, handler domain.AgentEventHandler) error {
	s.wsConnected.Store(false)
	s.wsConnectedAt.Store(0)
	defer func() {
		s.wsConnected.Store(false)
		s.wsConnectedAt.Store(0)
	}()
	// Clear busy on disconnect — the final frame may never arrive.
	defer s.activeTurn.Store(false)
	defer s.clearTurn()

	connStart := flow.Start("ws_connect", map[string]any{"url": WSURL})

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	header := http.Header{}
	header.Set("Authorization", "Bearer "+Token)
	conn, resp, err := dialer.DialContext(ctx, WSURL, header)
	if err != nil {
		if resp != nil {
			flow.End("ws_connect", connStart, map[string]any{"error": err.Error(), "status": resp.Status})
			return fmt.Errorf("dial %s: %w (status %s)", WSURL, err, resp.Status)
		}
		flow.End("ws_connect", connStart, map[string]any{"error": err.Error()})
		return fmt.Errorf("dial %s: %w", WSURL, err)
	}
	defer func() {
		s.wsMu.Lock()
		s.wsConn = nil
		s.wsMu.Unlock()
		conn.Close()
	}()

	s.wsMu.Lock()
	s.wsConn = conn
	s.wsMu.Unlock()
	s.wsConnected.Store(true)
	s.wsConnectedAt.Store(time.Now().Unix())
	if s.statusLED != nil && s.config.SetUpCompleted {
		s.statusLED.Clear(statusled.StateAgentDown)
	}
	flow.End("ws_connect", connStart, map[string]any{"connected": true})
	flow.Log("ws_ready", map[string]any{"backend": "picoclaw"})
	slog.Info("PicoClaw connected", "component", "picoclaw", "url", WSURL)

	// On reconnect (not first boot), announce via TTS so the user knows the agent
	// is back. hal.Speak (not SendToHALTTS): hardcoded system filler, must NOT be
	// fed to the realtime voice agent as history.
	if s.wsHasConnected.Swap(true) {
		go func() {
			phrase := i18n.Pick(i18n.PhraseReconnect)
			if err := hal.Speak(phrase); err != nil {
				slog.Warn("reconnect TTS failed", "component", "picoclaw", "error", err)
			}
		}()
	}

	// Keepalive ping loop — bounded to this connection's lifetime.
	pingCtx, cancelPing := context.WithCancel(ctx)
	defer cancelPing()
	go s.keepAlive(pingCtx)

	dispatch := func(evt domain.WSEvent) {
		if handler == nil {
			return
		}
		// Best-effort: drop handler errors but keep reading (matches openclaw).
		if err := handler(ctx, evt); err != nil {
			slog.Error("ws handler error", "component", "picoclaw", "event", evt.Event, "error", err)
		}
	}

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		conn.SetReadDeadline(time.Now().Add(readDeadline))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		s.translateFrame(msg, dispatch)
	}
}

// keepAlive sends an application-level ping every pingInterval. PicoClaw replies
// with a pong frame (ignored by the translator) which refreshes the read
// deadline and keeps an idle socket alive.
func (s *PicoclawService) keepAlive(ctx context.Context) {
	tick := time.NewTicker(pingInterval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			if err := s.sendFrame(map[string]any{
				"type": "ping",
				"id":   fmt.Sprintf("ping-%d", s.reqCounter.Add(1)),
			}); err != nil {
				// Write failure means the socket is gone; the read loop will see
				// the same error and trigger reconnect. Nothing to do here.
				return
			}
		}
	}
}

// sendFrame marshals v and writes it to the WebSocket under wsMu. Returns an
// error when the socket is not connected.
func (s *PicoclawService) sendFrame(v any) error {
	body, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal frame: %w", err)
	}
	s.wsMu.Lock()
	conn := s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		return fmt.Errorf("picoclaw websocket not connected")
	}
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		return fmt.Errorf("write frame: %w", err)
	}
	return nil
}

// sleepCtx sleeps for d or until ctx is cancelled. Returns false if cancelled.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
