package openclaw

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"go.autonomous.ai/os/system/monitor"
)

func TestPendingTraceRetentionAndBusyWindow(t *testing.T) {
	s := &OpenclawService{}
	s.pendingTaskBuf = []pendingTrace{{runID: "queued", message: "queued task", sentAt: time.Now().Add(-3 * time.Minute)}}
	if s.HasFreshPendingChatSend() {
		t.Fatal("old correlation evidence extended busy window")
	}
	s.pendingChatBuf = append([]pendingTrace(nil), s.pendingTaskBuf...)
	if got := s.MatchPendingByMessage("queued task"); got != "" {
		t.Fatalf("routing retention changed: %q", got)
	}
	if got := s.MatchPendingTaskByMessage(" queued task "); got != "queued" {
		t.Fatalf("queued trace lost: %q", got)
	}
	s.SetPendingChatTrace("fresh", "new task")
	if !s.HasFreshPendingChatSend() {
		t.Fatal("fresh send must retain busy protection")
	}
	s.pendingChatBuf[0].sentAt = time.Now().Add(-pendingSendBusyWindow - time.Second)
	if s.HasFreshPendingChatSend() {
		t.Fatal("busy window exceeded 30 seconds")
	}
	if got := s.MatchPendingTaskByMessage("new task"); got != "fresh" {
		t.Fatalf("busy expiry discarded trace: %q", got)
	}
}

func TestPendingTraceExpiryAndCapacity(t *testing.T) {
	s := &OpenclawService{}
	s.pendingTaskBuf = []pendingTrace{{runID: "expired", message: "old", sentAt: time.Now().Add(-pendingTaskTTL - time.Second)}}
	if got := s.MatchPendingTaskByMessage("old"); got != "" {
		t.Fatalf("expired trace matched: %q", got)
	}
	for i := 0; i < pendingTaskMaxEntries+1; i++ {
		s.SetPendingChatTrace(fmt.Sprint(i), fmt.Sprintf("task-%d", i))
	}
	if len(s.pendingTaskBuf) != pendingTaskMaxEntries {
		t.Fatalf("unbounded traces: %d", len(s.pendingTaskBuf))
	}
	if got := s.MatchPendingTaskByMessage("task-0"); got != "" {
		t.Fatalf("oldest trace retained over cap: %q", got)
	}
	if got := s.MatchPendingTaskByMessage(fmt.Sprintf("task-%d", pendingTaskMaxEntries)); got != fmt.Sprint(pendingTaskMaxEntries) {
		t.Fatalf("newest trace lost: %q", got)
	}
}

func TestPendingTraceRequiresUniqueExactMessage(t *testing.T) {
	s := &OpenclawService{}
	s.SetPendingChatTrace("first", "Hello")
	s.SetPendingChatTrace("second", " Hello ")
	if got := s.MatchPendingTaskByMessage("Hello"); got != "" {
		t.Fatalf("ambiguous text guessed a run: %q", got)
	}
	if len(s.pendingTaskBuf) != 2 {
		t.Fatal("ambiguous evidence consumed")
	}
	prefix := strings.Repeat("a", 256)
	s.SetPendingChatTrace("long", prefix+" different task")
	if got := s.MatchPendingTaskByMessage(prefix + " another task"); got != "" {
		t.Fatalf("prefix guessed a run: %q", got)
	}
	if got := s.MatchPendingTaskByMessage("Hell"); got != "" {
		t.Fatalf("short prefix guessed a run: %q", got)
	}
	if got := s.MatchPendingTaskByMessage(prefix + " different task"); got != "long" {
		t.Fatalf("unique exact match lost: %q", got)
	}
}

type pendingTraceWriteConn struct {
	net.Conn
	beforeWrite func()
}

func (c *pendingTraceWriteConn) Write(p []byte) (int, error) {
	if c.beforeWrite != nil {
		c.beforeWrite()
	}
	return c.Conn.Write(p)
}

func pendingTraceSocket(t *testing.T) (*websocket.Conn, *pendingTraceWriteConn) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer peer.Close()
		_, _, _ = peer.ReadMessage()
	}))
	t.Cleanup(server.Close)
	var wrapped *pendingTraceWriteConn
	dialer := websocket.Dialer{NetDial: func(network, addr string) (net.Conn, error) {
		conn, err := net.Dial(network, addr)
		if err != nil {
			return nil, err
		}
		wrapped = &pendingTraceWriteConn{Conn: conn}
		return wrapped, nil
	}}
	conn, _, err := dialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn, wrapped
}

func TestSendChatRegistersWireTraceBeforeWrite(t *testing.T) {
	conn, wrapped := pendingTraceSocket(t)
	s := &OpenclawService{wsConn: conn, monitorBus: monitor.ProvideBus()}
	message := "[sensing:presence.enter] hello [snapshot: /tmp/face.jpg]"
	wireMessage := strings.TrimSpace(reSnapshotPath.ReplaceAllString(message, ""))
	if wireMessage == message {
		t.Fatal("fixture did not exercise stripped wire message")
	}
	var matched string
	wrapped.beforeWrite = func() {
		matched = s.MatchPendingTaskByMessage(wireMessage)
		s.MatchPendingByMessage(wireMessage)
	}
	if _, err := s.SendChatMessageWithRun(message, "req-1", "device-chat-test"); err != nil {
		t.Fatal(err)
	}
	if matched != "device-chat-test" {
		t.Fatalf("early lifecycle missed wire trace: %q", matched)
	}
	if len(s.pendingChatBuf) != 0 || len(s.pendingTaskBuf) != 0 {
		t.Fatal("send recreated trace already consumed by lifecycle")
	}
}

func TestSendChatWriteFailureRemovesOnlyItsTrace(t *testing.T) {
	conn, _ := pendingTraceSocket(t)
	s := &OpenclawService{wsConn: conn, monitorBus: monitor.ProvideBus()}
	s.SetPendingChatTrace("other", "other task")
	_ = conn.Close()
	if _, err := s.SendChatMessageWithRun("failed task", "req-failed", "device-chat-failed"); err == nil {
		t.Fatal("expected closed socket failure")
	}
	if got := s.MatchPendingTaskByMessage("failed task"); got != "" {
		t.Fatalf("failed send left trace: %q", got)
	}
	if got := s.MatchPendingByMessage("other task"); got != "other" {
		t.Fatalf("failed send removed unrelated trace: %q", got)
	}
}

func TestPendingTaskMatchingPreservesRoutingHeuristics(t *testing.T) {
	s := &OpenclawService{}
	s.SetPendingChatTrace("first", "hello")
	s.SetPendingChatTrace("second", "hello")
	if got := s.MatchPendingTaskByMessage("hello"); got != "" {
		t.Fatalf("ambiguous task guessed: %q", got)
	}
	if got := s.MatchPendingByMessage("hello"); got != "first" {
		t.Fatalf("routing FIFO changed: %q", got)
	}
	if got := s.MatchPendingTaskByMessage("hello"); got != "" {
		t.Fatalf("routing disambiguated telemetry without evidence: %q", got)
	}
	if got := s.MatchPendingByMessage("hel"); got != "second" {
		t.Fatalf("routing prefix changed: %q", got)
	}
	s.SetPendingChatTrace("unique", "unique message")
	if got := s.MatchPendingTaskByMessage("unique message"); got != "unique" {
		t.Fatalf("unique task lost: %q", got)
	}
	if got := s.MatchPendingByMessage("unique message"); got != "unique" {
		t.Fatalf("telemetry consumed routing: %q", got)
	}
}

func TestRemovePendingTraceClearsExpiredRoutingTaskEvidence(t *testing.T) {
	s := &OpenclawService{}
	s.SetPendingChatTrace("expired-routing", "old task")
	s.pendingChatBuf[0].sentAt = time.Now().Add(-pendingChatTTL - time.Second)
	if s.RemovePendingChatTraceByRunID("expired-routing") {
		t.Fatal("expired routing changed control result")
	}
	if got := s.MatchPendingTaskByMessage("old task"); got != "" {
		t.Fatalf("terminal left orphan task evidence: %q", got)
	}
}
