package buddy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
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
// overall timeout via ctx; command timeouts are bounded to one minute.
func (d *Dispatcher) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	if cmd.TimeoutMs != 0 && (cmd.TimeoutMs < 500 || cmd.TimeoutMs > 60000) {
		return nil, errors.New("timeout_ms must be 0 or from 500 to 60000")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	conn := d.registry.Conn()
	if conn == nil {
		slog.Warn("buddy dispatch: no buddy connected", "component", "buddy", "action", cmd.Action)
		return nil, ErrNoBuddyConnected
	}
	if cmd.ID == "" {
		cmd.ID = NewCommandID()
	}

	ch, err := d.registry.RegisterPending(conn, cmd.ID)
	if err != nil {
		return nil, err
	}
	defer d.registry.CancelPending(cmd.ID, ch)

	data, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("marshal command: %w", err)
	}
	slog.Info("buddy dispatch → WS write", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "bytes", len(data))
	if err := d.registry.Write(ctx, conn, data); err != nil {
		slog.Warn("buddy dispatch: WS write failed", "component", "buddy", "id", cmd.ID, "error", err)
		return nil, fmt.Errorf("write WS: %w", err)
	}

	// Wait for response OR ctx cancel OR hard timeout (in case caller passed background ctx).
	timeout := 30 * time.Second
	if cmd.TimeoutMs > 0 {
		timeout = time.Duration(cmd.TimeoutMs)*time.Millisecond + 5*time.Second
	}
	start := time.Now()
	select {
	case resp := <-ch:
		if resp == nil {
			return nil, errors.New("buddy disconnected during command; observe before retrying")
		}
		slog.Info("buddy dispatch ← response", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "elapsed_ms", time.Since(start).Milliseconds(), "bytes", len(resp))
		return resp, nil
	case <-ctx.Done():
		slog.Warn("buddy dispatch: ctx cancel", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "elapsed_ms", time.Since(start).Milliseconds(), "error", ctx.Err())
		d.cancelCommand(conn, cmd)
		return nil, ctx.Err()
	case <-time.After(timeout):
		slog.Warn("buddy dispatch: timeout", "component", "buddy", "id", cmd.ID, "action", cmd.Action, "elapsed_ms", time.Since(start).Milliseconds())
		d.cancelCommand(conn, cmd)
		return nil, ErrBuddyTimeout
	}
}

// Cancellation is best effort: the command may already have affected the UI.
// Never replay an interrupted input automatically, and never cancel on a new socket.
func (d *Dispatcher) cancelCommand(conn *websocket.Conn, cmd Command) {
	if cmd.Action == "cancel_command" {
		return
	}
	cancel := Command{ID: NewCommandID(), Action: "cancel_command", Params: map[string]any{"id": cmd.ID}, TimeoutMs: 1000}
	data, err := json.Marshal(cancel)
	if err != nil {
		return
	}
	ctx, stop := context.WithTimeout(context.Background(), time.Second)
	defer stop()
	if err := d.registry.Write(ctx, conn, data); err != nil {
		slog.Warn("buddy cancellation not delivered", "component", "buddy", "id", cmd.ID, "error", err)
	}
}
