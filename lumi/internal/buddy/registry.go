package buddy

import (
	"encoding/json"
	"sync"

	"github.com/gorilla/websocket"
)

// Registry tracks the (single) connected buddy WebSocket and the table of in-flight
// requests waiting for their matching response. MVP is 1↔1; future multi-buddy
// will key by buddy ID.
type Registry struct {
	mu      sync.Mutex
	conn    *websocket.Conn
	pending map[string]chan json.RawMessage
}

func NewRegistry() *Registry {
	return &Registry{pending: make(map[string]chan json.RawMessage)}
}

// Set replaces the active connection. Closes the previous one if present.
func (r *Registry) Set(c *websocket.Conn) {
	r.mu.Lock()
	old := r.conn
	r.conn = c
	r.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
}

// Clear drops the active connection without closing it (caller closes on exit).
func (r *Registry) Clear() {
	r.mu.Lock()
	r.conn = nil
	r.mu.Unlock()
}

// Conn returns the current connection or nil.
func (r *Registry) Conn() *websocket.Conn {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.conn
}

// RegisterPending creates a one-shot channel that will receive the response
// keyed by `id`. Caller must defer CancelPending to clean up.
func (r *Registry) RegisterPending(id string) chan json.RawMessage {
	ch := make(chan json.RawMessage, 1)
	r.mu.Lock()
	r.pending[id] = ch
	r.mu.Unlock()
	return ch
}

// DeliverResponse routes a response from the WS reader loop to the waiting
// Dispatch caller. Returns false if there is no pending registration for `id`.
func (r *Registry) DeliverResponse(id string, body json.RawMessage) bool {
	r.mu.Lock()
	ch, ok := r.pending[id]
	if ok {
		delete(r.pending, id)
	}
	r.mu.Unlock()
	if !ok {
		return false
	}
	select {
	case ch <- body:
	default:
	}
	return true
}

// CancelPending removes a pending registration (e.g. on Dispatch timeout).
func (r *Registry) CancelPending(id string) {
	r.mu.Lock()
	delete(r.pending, id)
	r.mu.Unlock()
}
