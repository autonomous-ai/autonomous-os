package buddy

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func socketPair(t *testing.T) (*websocket.Conn, *websocket.Conn) {
	t.Helper()
	accepted := make(chan *websocket.Conn, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err == nil {
			accepted <- c
		}
	}))
	t.Cleanup(server.Close)
	client, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	var peer *websocket.Conn
	select {
	case peer = <-accepted:
	case <-time.After(time.Second):
		t.Fatal("upgrade timeout")
	}
	t.Cleanup(func() { client.Close(); peer.Close() })
	return client, peer
}

func TestReconnectKeepsNewConnectionAndFailsOldPending(t *testing.T) {
	old, _ := socketPair(t)
	next, _ := socketPair(t)
	r := NewRegistry()
	r.Set(old)
	pending, err := r.RegisterPending(old, "old")
	if err != nil {
		t.Fatal(err)
	}
	r.Set(next)
	r.ClearConnection(old)
	if r.Conn() != next {
		t.Fatal("old reader removed replacement connection")
	}
	select {
	case got := <-pending:
		if got != nil {
			t.Fatal("expected disconnection")
		}
	case <-time.After(time.Second):
		t.Fatal("pending caller not released")
	}
	current, err := r.RegisterPending(next, "same")
	if err != nil {
		t.Fatal(err)
	}
	if r.DeliverResponse(old, "same", json.RawMessage(`{"ok":true}`)) {
		t.Fatal("stale connection completed current request")
	}
	if !r.DeliverResponse(next, "same", json.RawMessage(`{"ok":true}`)) {
		t.Fatal("current response rejected")
	}
	<-current
}

func TestDuplicateIDAndOldCleanupCannotReplaceNewCaller(t *testing.T) {
	conn, _ := socketPair(t)
	r := NewRegistry()
	r.Set(conn)
	first, err := r.RegisterPending(conn, "id")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := r.RegisterPending(conn, "id"); err == nil {
		t.Fatal("duplicate ID accepted")
	}
	r.DeliverResponse(conn, "id", json.RawMessage(`{}`))
	second, err := r.RegisterPending(conn, "id")
	if err != nil {
		t.Fatal(err)
	}
	r.CancelPending("id", first)
	if !r.DeliverResponse(conn, "id", json.RawMessage(`{}`)) {
		t.Fatal("old defer deleted new pending request")
	}
	<-second
}

func TestConcurrentDispatchMatchesResponsesWithoutConcurrentWrites(t *testing.T) {
	conn, peer := socketPair(t)
	registry := NewRegistry()
	registry.Set(conn)
	svc := &Service{registry: registry}
	done := make(chan struct{})
	go func() { defer close(done); svc.RunReadLoop(conn, "test") }()
	t.Cleanup(func() { peer.Close(); <-done })
	const count = 24
	go func() {
		for i := 0; i < count; i++ {
			var cmd Command
			if peer.ReadJSON(&cmd) != nil {
				return
			}
			if peer.WriteJSON(CommandResponse{ID: cmd.ID, OK: true, Result: map[string]any{"action": cmd.Action}}) != nil {
				return
			}
		}
	}()
	dispatcher := NewDispatcher(registry)
	var wg sync.WaitGroup
	for i := 0; i < count; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			raw, err := dispatcher.Dispatch(ctx, Command{Action: "ping"})
			if err != nil {
				t.Error(err)
				return
			}
			var response CommandResponse
			if json.Unmarshal(raw, &response) != nil || !response.OK || response.Result["action"] != "ping" {
				t.Errorf("bad response %s", raw)
			}
		}()
	}
	wg.Wait()
}

func TestCancelledDispatchSendsTargetedCancelWithoutReplay(t *testing.T) {
	conn, peer := socketPair(t)
	registry := NewRegistry()
	registry.Set(conn)
	dispatcher := NewDispatcher(registry)
	seen := make(chan Command, 2)
	go func() {
		for i := 0; i < 2; i++ {
			var cmd Command
			if peer.ReadJSON(&cmd) != nil {
				return
			}
			seen <- cmd
		}
	}()
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan error, 1)
	go func() {
		_, err := dispatcher.Dispatch(ctx, Command{ID: "original", Action: "type_text"})
		finished <- err
	}()
	select {
	case cmd := <-seen:
		if cmd.Action != "type_text" {
			t.Fatal(cmd)
		}
	case <-time.After(time.Second):
		t.Fatal("no input command")
	}
	cancel()
	select {
	case cmd := <-seen:
		if cmd.Action != "cancel_command" || cmd.Params["id"] != "original" || cmd.ID == "original" {
			t.Fatal(cmd)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no targeted cancellation")
	}
	if err := <-finished; err != context.Canceled {
		t.Fatalf("got %v", err)
	}
}

func TestRejectInvalidTimeoutBeforeDispatch(t *testing.T) {
	d := NewDispatcher(NewRegistry())
	for _, timeout := range []int{-1, 1, 499, 60001, int(^uint(0) >> 1)} {
		_, err := d.Dispatch(context.Background(), Command{Action: "ping", TimeoutMs: timeout})
		if err == nil || err == ErrNoBuddyConnected {
			t.Fatalf("invalid timeout %d not validated", timeout)
		}
	}
}
