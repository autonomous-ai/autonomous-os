package buddy

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type pendingCommand struct {
	conn     *websocket.Conn
	response chan json.RawMessage
}

// Registry owns one paired connection. Pending responses belong to the exact
// connection that received the command, so a reconnect cannot complete old work.
type Registry struct {
	mu      sync.Mutex
	conn    *websocket.Conn
	pending map[string]pendingCommand
	writer  chan struct{}
}

func NewRegistry() *Registry {
	return &Registry{pending: make(map[string]pendingCommand), writer: make(chan struct{}, 1)}
}

func (r *Registry) Set(c *websocket.Conn) {
	r.mu.Lock()
	old := r.conn
	r.conn = c
	if old != c {
		r.cancelConnectionLocked(old)
	}
	r.mu.Unlock()
	if old != nil && old != c {
		_ = old.Close()
	}
}

func (r *Registry) cancelConnectionLocked(conn *websocket.Conn) {
	for id, pending := range r.pending {
		if pending.conn == conn {
			pending.response <- nil
			delete(r.pending, id)
		}
	}
}

// Clear removes all current state (used when unpairing).
func (r *Registry) Clear() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.cancelConnectionLocked(r.conn)
	r.conn = nil
}

// ClearConnection ignores a stale reader exiting after a replacement connected.
func (r *Registry) ClearConnection(conn *websocket.Conn) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.cancelConnectionLocked(conn)
	if r.conn == conn {
		r.conn = nil
	}
}

func (r *Registry) Conn() *websocket.Conn {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.conn
}

func (r *Registry) RegisterPending(conn *websocket.Conn, id string) (chan json.RawMessage, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if conn == nil || r.conn != conn {
		return nil, ErrNoBuddyConnected
	}
	if _, exists := r.pending[id]; exists {
		return nil, errors.New("duplicate in-flight command id")
	}
	ch := make(chan json.RawMessage, 1)
	r.pending[id] = pendingCommand{conn: conn, response: ch}
	return ch, nil
}

func (r *Registry) DeliverResponse(conn *websocket.Conn, id string, body json.RawMessage) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	pending, ok := r.pending[id]
	if !ok || pending.conn != conn {
		return false
	}
	delete(r.pending, id)
	pending.response <- body
	return true
}

func (r *Registry) CancelPending(id string, response chan json.RawMessage) {
	r.mu.Lock()
	if pending, ok := r.pending[id]; ok && pending.response == response {
		delete(r.pending, id)
	}
	r.mu.Unlock()
}

// Write serializes data frames and bounds socket backpressure. Control frames
// (including Gorilla's ping replies) are safe concurrently with this writer.
func (r *Registry) Write(ctx context.Context, conn *websocket.Conn, data []byte) error {
	select {
	case r.writer <- struct{}{}:
		defer func() { <-r.writer }()
	case <-ctx.Done():
		return ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if conn == nil || r.Conn() != conn {
		return ErrNoBuddyConnected
	}
	deadline := time.Now().Add(5 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	if err := conn.SetWriteDeadline(deadline); err != nil {
		return err
	}
	return conn.WriteMessage(websocket.TextMessage, data)
}
