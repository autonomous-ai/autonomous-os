package gatewayd

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const testToken = "test-token"

// successJSONL is the canned codex --json output emitted by the fake binary.
const successJSONL = `{"type":"thread.started","thread_id":"t123"}
{"type":"item.completed","item":{"item_type":"agent_message","text":"hello"}}
{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}`

// writeFakeCodex writes a bash script that appends its argv to argvFile
// (one "ARGV:<space-joined args>" line per invocation) and emits the canned
// success JSONL on stdout.
func writeFakeCodex(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf("#!/bin/bash\necho \"ARGV:$*\" >> %q\ncat <<'EOF'\n%s\nEOF\n",
		argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex", script)
}

// writeFakeCodexResumeFails is a variant that fails resume attempts with
// "session not found" on stderr and succeeds on fresh runs.
func writeFakeCodexResumeFails(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *resume*)
    echo "session not found" >&2
    exit 1
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex-resume-fails", script)
}

// writeFakeCodexResumeTurnFailed fails resume attempts with a terminal
// turn.failed JSONL frame on stdout (codex's missing-thread shape) + exit 1,
// and emits the canned success on fresh runs.
func writeFakeCodexResumeTurnFailed(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *resume*)
    echo '{"type":"turn.failed","error":{"message":"no rollout found for thread id x"}}'
    exit 1
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex-resume-turn-failed", script)
}

// writeFakeCodexResumeHangs answers a fresh run normally but never returns on a
// resume — the shape measured on lamp-0c89 2026-09-03, where the upstream
// stream went silent mid-turn and `codex exec ... resume <id>` sat on an open
// socket until it was killed.
func writeFakeCodexResumeHangs(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *resume*)
    sleep 30
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex-resume-hangs", script)
}

// writeFakeCodexSlowButTalking takes far longer than the idle window overall but
// never stops narrating — the shape of a genuinely long turn (a tool call every
// few seconds). It must NOT be killed.
func writeFakeCodexSlowButTalking(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
for i in 1 2 3 4 5 6 7 8; do
  echo '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"working"}}'
  sleep 0.4
done
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex-slow-talking", script)
}

// writeFakeCodexSilent starts a turn and then produces nothing — the wedge
// measured on lamp-0c89 2026-09-03.
func writeFakeCodexSilent(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
echo '{"type":"thread.started","thread_id":"t123"}'
sleep 30
`, argvFile)
	return writeScript(t, dir, "fake-codex-silent", script)
}

func writeScript(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatalf("write fake codex: %v", err)
	}
	return path
}

// startServer boots a Server on an ephemeral loopback port with all paths
// under t.TempDir(). Returns the ws URL and the config used.
func startServer(t *testing.T, codexBin string, dir string) (string, Config) {
	t.Helper()
	return startServerTimeout(t, codexBin, dir, 30*time.Second)
}

// startServerTimeout is startServer with an explicit per-turn timeout, for the
// tests that need a turn to actually hit it.
func startServerTimeout(t *testing.T, codexBin string, dir string, turnTimeout time.Duration) (string, Config) {
	t.Helper()
	return startServerTimeouts(t, codexBin, dir, turnTimeout, 0)
}

// startServerTimeouts sets both guards: the absolute ceiling and the idle one
// (0 disables the idle guard).
func startServerTimeouts(t *testing.T, codexBin string, dir string, turnTimeout, idleTimeout time.Duration) (string, Config) {
	t.Helper()
	cfg := Config{
		Token:           testToken,
		Workspace:       filepath.Join(dir, "workspace"),
		CodexBin:        codexBin,
		CodexHome:       dir,
		SessionFile:     filepath.Join(dir, "session.json"),
		AttachDir:       filepath.Join(dir, "attachments"),
		TurnTimeout:     turnTimeout,
		TurnIdleTimeout: idleTimeout,
		Home:            dir,
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := New(cfg, ln)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := srv.Serve(ctx); err != nil {
			t.Errorf("serve: %v", err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})
	return "ws://" + ln.Addr().String() + "/codex/ws", cfg
}

func dial(t *testing.T, url, token string) *websocket.Conn {
	t.Helper()
	header := http.Header{"Authorization": {"Bearer " + token}}
	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func readFrame(t *testing.T, conn *websocket.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("unmarshal frame %q: %v", data, err)
	}
	return frame
}

func sendMessage(t *testing.T, conn *websocket.Conn, content string) {
	t.Helper()
	frame := fmt.Sprintf(`{"type":"message.send","id":1,"payload":{"content":%q}}`, content)
	if err := conn.WriteMessage(websocket.TextMessage, []byte(frame)); err != nil {
		t.Fatalf("send message.send: %v", err)
	}
}

// readTurnFrames reads frames until turn.completed/turn.failed/bridge.error,
// returning the codex-event frames in order (bridge.status is skipped).
func readTurnFrames(t *testing.T, conn *websocket.Conn) []map[string]any {
	t.Helper()
	var frames []map[string]any
	for {
		frame := readFrame(t, conn)
		typ, _ := frame["type"].(string)
		if typ == "bridge.status" {
			continue
		}
		frames = append(frames, frame)
		if typ == "turn.completed" || typ == "turn.failed" || typ == "bridge.error" {
			return frames
		}
	}
}

// argvLines returns the "ARGV:" lines the fake codex appended per invocation.
func argvLines(t *testing.T, argvFile string) []string {
	t.Helper()
	data, err := os.ReadFile(argvFile)
	if err != nil {
		t.Fatalf("read argv file: %v", err)
	}
	var lines []string
	for _, l := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if strings.HasPrefix(l, "ARGV:") {
			lines = append(lines, strings.TrimPrefix(l, "ARGV:"))
		}
	}
	return lines
}

func readSessionThreadID(t *testing.T, sessionFile string) string {
	t.Helper()
	data, err := os.ReadFile(sessionFile)
	if err != nil {
		t.Fatalf("read session file: %v", err)
	}
	var sess struct {
		ThreadID string `json:"thread_id"`
	}
	if err := json.Unmarshal(data, &sess); err != nil {
		t.Fatalf("unmarshal session file %q: %v", data, err)
	}
	return sess.ThreadID
}

func TestHappyPath(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, cfg := startServer(t, writeFakeCodex(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)

	// First frame is bridge.status ready with empty thread id.
	status := readFrame(t, conn)
	if status["type"] != "bridge.status" || status["state"] != "ready" {
		t.Fatalf("expected ready status, got %v", status)
	}
	if status["thread_id"] != "" {
		t.Fatalf("expected empty thread_id on fresh start, got %v", status["thread_id"])
	}

	sendMessage(t, conn, "hi codex")
	frames := readTurnFrames(t, conn)
	if len(frames) != 3 {
		t.Fatalf("expected 3 codex frames, got %d: %v", len(frames), frames)
	}
	wantTypes := []string{"thread.started", "item.completed", "turn.completed"}
	for i, want := range wantTypes {
		if frames[i]["type"] != want {
			t.Fatalf("frame %d: expected type %q, got %v", i, want, frames[i])
		}
	}
	if frames[0]["thread_id"] != "t123" {
		t.Fatalf("expected thread_id t123, got %v", frames[0]["thread_id"])
	}
	if got := readSessionThreadID(t, cfg.SessionFile); got != "t123" {
		t.Fatalf("session file: expected t123, got %q", got)
	}
	// The prompt must be the final positional argument of a fresh run.
	lines := argvLines(t, argvFile)
	if len(lines) != 1 || strings.Contains(lines[0], "resume") ||
		!strings.HasSuffix(lines[0], "hi codex") {
		t.Fatalf("unexpected fresh argv: %v", lines)
	}
}

func TestResumeUsesStoredThreadID(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServer(t, writeFakeCodex(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	sendMessage(t, conn, "first")
	readTurnFrames(t, conn)
	sendMessage(t, conn, "second")
	readTurnFrames(t, conn)

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected 2 codex invocations, got %d: %v", len(lines), lines)
	}
	if strings.Contains(lines[0], "resume") {
		t.Fatalf("first run must be fresh, got argv: %s", lines[0])
	}
	if !strings.Contains(lines[1], "resume t123") {
		t.Fatalf("second run must resume t123, got argv: %s", lines[1])
	}
}

func TestSessionNewClearsSession(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, cfg := startServer(t, writeFakeCodex(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	// session.new races an in-flight turn: it rides the same worker queue, so
	// it must execute AFTER the turn — the turn's thread.started re-persist
	// cannot undo the clear.
	sendMessage(t, conn, "first")
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"session.new"}`)); err != nil {
		t.Fatalf("send session.new: %v", err)
	}

	// Ordered stream: the full turn first (thread persisted from
	// thread.started), THEN the session_cleared status.
	sawCompleted := false
	for {
		frame := readFrame(t, conn)
		switch typ, _ := frame["type"].(string); typ {
		case "bridge.status":
			if frame["state"] != "session_cleared" {
				continue
			}
			if !sawCompleted {
				t.Fatalf("session_cleared must arrive after turn.completed, got %v early", frame)
			}
			if frame["thread_id"] != "" {
				t.Fatalf("expected empty thread_id in session_cleared, got %v", frame)
			}
		case "thread.started":
			if frame["thread_id"] != "t123" {
				t.Fatalf("expected thread.started t123, got %v", frame)
			}
			continue
		case "turn.completed":
			sawCompleted = true
			continue
		case "turn.failed", "bridge.error":
			t.Fatalf("unexpected failure frame: %v", frame)
		default:
			continue
		}
		break
	}
	if _, err := os.Stat(cfg.SessionFile); !os.IsNotExist(err) {
		t.Fatalf("session file should be deleted after session_cleared, stat err: %v", err)
	}

	sendMessage(t, conn, "after reset")
	readTurnFrames(t, conn)
	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected 2 codex invocations, got %d: %v", len(lines), lines)
	}
	if strings.Contains(lines[0], "resume") {
		t.Fatalf("first run must be fresh, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "resume") {
		t.Fatalf("run after session.new must be fresh, got argv: %s", lines[1])
	}
}

func TestAuthRejectsWrongToken(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServer(t, writeFakeCodex(t, dir, argvFile), dir)

	header := http.Header{"Authorization": {"Bearer wrong-token"}}
	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		return // handshake rejected outright is acceptable too
	}
	defer conn.Close()
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, _, err = conn.ReadMessage()
	if err == nil {
		t.Fatal("expected connection to be closed for wrong token")
	}
	var closeErr *websocket.CloseError
	if ok := websocket.IsCloseError(err, closeUnauthorized); !ok {
		if e, isClose := err.(*websocket.CloseError); isClose {
			closeErr = e
		}
		t.Fatalf("expected close code %d, got %v (close: %v)", closeUnauthorized, err, closeErr)
	}
}

func TestResumeRetryFresh(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	// Seed a stale session so the first run tries (and fails) to resume it.
	sessionFile := filepath.Join(dir, "session.json")
	if err := os.WriteFile(sessionFile, []byte(`{"thread_id":"stale-999"}`), 0o600); err != nil {
		t.Fatalf("seed session file: %v", err)
	}
	url, cfg := startServer(t, writeFakeCodexResumeFails(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)

	status := readFrame(t, conn) // ready status carries the stale thread id
	if status["thread_id"] != "stale-999" {
		t.Fatalf("expected seeded thread_id, got %v", status["thread_id"])
	}

	sendMessage(t, conn, "retry me")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "turn.completed" {
		t.Fatalf("expected turn.completed after fresh retry, got %v", frames)
	}

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected resume attempt + fresh retry, got %d: %v", len(lines), lines)
	}
	if !strings.Contains(lines[0], "resume stale-999") {
		t.Fatalf("first run must resume stale id, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "resume") {
		t.Fatalf("retry must be fresh, got argv: %s", lines[1])
	}
	if got := readSessionThreadID(t, cfg.SessionFile); got != "t123" {
		t.Fatalf("session file after retry: expected t123, got %q", got)
	}
}

func TestResumedFailureFramesHeldBack(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	// Seed a stale session so the first run resumes and dies with turn.failed.
	sessionFile := filepath.Join(dir, "session.json")
	if err := os.WriteFile(sessionFile, []byte(`{"thread_id":"x"}`), 0o600); err != nil {
		t.Fatalf("seed session file: %v", err)
	}
	url, _ := startServer(t, writeFakeCodexResumeTurnFailed(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	sendMessage(t, conn, "retry me")
	frames := readTurnFrames(t, conn)
	completed := 0
	for _, f := range frames {
		switch f["type"] {
		case "turn.failed", "bridge.error":
			// The resumed attempt's terminal failure must be held back — the
			// fresh retry owns the turn on the client side.
			t.Fatalf("client must not see the resumed attempt's failure: %v", f)
		case "turn.completed":
			completed++
		}
	}
	if completed != 1 {
		t.Fatalf("expected exactly one turn.completed, got %d: %v", completed, frames)
	}

	// Nothing stashed may trail the completed turn: a ping must be answered next.
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"ping","id":9}`)); err != nil {
		t.Fatalf("send ping: %v", err)
	}
	if frame := readFrame(t, conn); frame["type"] != "pong" {
		t.Fatalf("expected pong right after turn.completed, got %v", frame)
	}

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected resume attempt + fresh retry, got %d: %v", len(lines), lines)
	}
	if !strings.Contains(lines[0], "resume x") {
		t.Fatalf("first run must resume stale id, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "resume") {
		t.Fatalf("retry must be fresh, got argv: %s", lines[1])
	}
}

// A thread whose resume hangs used to wedge the device forever: rotation rides
// on a COMPLETED turn, so a thread that never completes one is never rotated,
// and the thread id lives on disk — restarting the service or rebooting the
// device resumed the same dead thread. The timeout must drop it.
func TestResumeTimeoutDropsTheThread(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, cfg := startServerTimeout(t, writeFakeCodexResumeHangs(t, dir, argvFile), dir, 500*time.Millisecond)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	// First turn is fresh and succeeds, persisting thread t123.
	sendMessage(t, conn, "first")
	readTurnFrames(t, conn)

	// Second turn resumes t123 and hangs until the turn timeout.
	sendMessage(t, conn, "second")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "bridge.error" {
		t.Fatalf("a timed-out turn must end with bridge.error, got %v", last)
	}

	if _, err := os.Stat(cfg.SessionFile); !os.IsNotExist(err) {
		t.Fatalf("the timed-out thread must be dropped, but %s still exists (err=%v)", cfg.SessionFile, err)
	}

	// Third turn must start FRESH — this is the escape the device never had.
	sendMessage(t, conn, "third")
	readTurnFrames(t, conn)
	lines := argvLines(t, argvFile)
	if len(lines) < 3 {
		t.Fatalf("expected at least 3 codex invocations, got %d: %v", len(lines), lines)
	}
	if strings.Contains(lines[2], "resume") {
		t.Fatalf("after a resume timeout the next turn must be fresh, got argv: %s", lines[2])
	}
}

// Total duration cannot tell a wedged turn from a slow one. A turn that keeps
// narrating must survive well past the idle window.
func TestIdleGuardSparesATurnThatKeepsTalking(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServerTimeouts(t, writeFakeCodexSlowButTalking(t, dir, argvFile), dir, 30*time.Second, 2*time.Second)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	sendMessage(t, conn, "long task")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "turn.completed" {
		t.Fatalf("a turn that keeps producing output must not be killed, got %v", last)
	}
}

// A turn that goes silent is killed by the idle guard long before the absolute
// ceiling — that is what frees the device.
func TestIdleGuardKillsASilentTurn(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServerTimeouts(t, writeFakeCodexSilent(t, dir, argvFile), dir, 30*time.Second, 2*time.Second)
	conn := dial(t, url, testToken)
	readFrame(t, conn) // ready status

	start := time.Now()
	sendMessage(t, conn, "wedged")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "bridge.error" {
		t.Fatalf("a silent turn must end with bridge.error, got %v", last)
	}
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("the idle guard must fire well before the absolute ceiling, took %s", elapsed)
	}
}
