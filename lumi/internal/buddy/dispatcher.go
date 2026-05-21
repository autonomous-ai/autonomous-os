package buddy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/gorilla/websocket"
)

// Dispatcher sends a command over the WebSocket and waits for the response with
// the matching ID, with overall timeout.
type Dispatcher struct {
	registry *Registry
}

func NewDispatcher(r *Registry) *Dispatcher {
	return &Dispatcher{registry: r}
}

var ErrNoBuddyConnected = errors.New("no buddy connected")
var ErrBuddyTimeout = errors.New("timeout waiting for buddy response")

// Dispatch returns the raw response JSON from the buddy. Caller decides the
// overall timeout via ctx (in addition to the per-command 10s floor).
func (d *Dispatcher) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	conn := d.registry.Conn()
	if conn == nil {
		return nil, ErrNoBuddyConnected
	}
	if cmd.ID == "" {
		cmd.ID = NewCommandID()
	}

	ch := d.registry.RegisterPending(cmd.ID)
	defer d.registry.CancelPending(cmd.ID)

	data, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("marshal command: %w", err)
	}
	if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
		return nil, fmt.Errorf("write WS: %w", err)
	}

	// Wait for response OR ctx cancel OR hard timeout (in case caller passed background ctx).
	timeout := 30 * time.Second
	if cmd.TimeoutMs > 0 {
		timeout = time.Duration(cmd.TimeoutMs)*time.Millisecond + 5*time.Second
	}
	select {
	case resp := <-ch:
		return resp, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(timeout):
		return nil, ErrBuddyTimeout
	}
}
