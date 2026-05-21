package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// Mirrors what lumi's `internal/buddy/ws.go` and `internal/buddy/dispatcher.go` will look like.

var upgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	// Buddy connects from same Mac (localhost) — keep permissive for dev.
	CheckOrigin: func(r *http.Request) bool { return true },
}

// HandleWS upgrades the request to WebSocket, validates the bearer token, then runs a
// reader loop that routes incoming responses to whoever is waiting in Dispatch().
func (s *State) HandleWS(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		http.Error(w, "missing bearer", http.StatusUnauthorized)
		return
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	record := s.lookupByToken(token)
	if record == nil {
		http.Error(w, "invalid token", http.StatusUnauthorized)
		return
	}

	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		logf("WS upgrade error: %v", err)
		return
	}
	s.setWS(ws)
	logf("✓ buddy connected: %s", record.BuddyID)

	defer func() {
		s.clearWS()
		_ = ws.Close()
		logf("buddy disconnected")
	}()

	ws.SetPongHandler(func(string) error { return nil })

	for {
		_, data, err := ws.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				logf("WS read error: %v", err)
			}
			return
		}
		var env struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(data, &env); err != nil || env.ID == "" {
			logf("malformed response: %s", string(data))
			continue
		}
		if !s.deliverResponse(env.ID, data) {
			logf("orphan response id=%s: %s", env.ID, string(data))
		}
	}
}

// Dispatch sends a command to the buddy over the open WS and waits for the response with the
// matching ID. Caller decides the overall timeout via ctx.
func (s *State) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	ws := s.currentWS()
	if ws == nil {
		return nil, errors.New("no buddy connected")
	}
	ch := s.registerPending(cmd.ID)
	defer s.cancelPending(cmd.ID)

	data, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}
	if err := ws.WriteMessage(websocket.TextMessage, data); err != nil {
		return nil, fmt.Errorf("write: %w", err)
	}

	select {
	case resp := <-ch:
		return resp, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(10 * time.Second):
		return nil, errors.New("timeout waiting for buddy response")
	}
}
