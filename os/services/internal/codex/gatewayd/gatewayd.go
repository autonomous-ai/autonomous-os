// Package gatewayd bridges a local WebSocket to per-turn `codex exec`
// subprocesses. It is a Go port of internal/codex/bridge.py (the reference
// semantics) and runs as `os-server codex-gatewayd` under the codex.service
// systemd unit (EnvironmentFile=/root/.codex/.env).
//
// Protocol (client = os-server internal/codex):
//
//	client -> gatewayd: {"type":"message.send","id":..,"payload":{"content":..,
//	                     "attachments":[{"type":"image","url":"data:<mt>;base64,<b64>"}]}}
//	                    {"type":"session.new"}  -> forget thread (runs after queued
//	                     turns), next turn is fresh
//	                    {"type":"ping","id":X}  -> {"type":"pong","id":X}
//	gatewayd -> client: codex `--json` JSONL events forwarded VERBATIM
//	                    (thread.started/item.*/turn.completed/turn.failed/..),
//	                    plus {"type":"pong"}, {"type":"bridge.status",..} and
//	                    {"type":"bridge.error","error":".."}.
//
// Turns are strictly serialized (buffered channel + single worker goroutine).
// The thread id from the thread.started event is persisted to the session
// file and replayed via `codex exec resume <thread_id>` on subsequent turns.
// Model/provider come from config.toml (presync-owned) — never --model here.
package gatewayd

import (
	"context"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const (
	logPrefix     = "[codex-gatewayd]"
	streamLimit   = 8 * 1024 * 1024 // max stdout line size (bufio.Scanner cap)
	scanBufSize   = 1024 * 1024     // initial bufio.Scanner buffer
	stderrTailMax = 4000            // bounded stderr tail kept for diagnostics
	attachMaxAge  = time.Hour       // best-effort cleanup of old attachments
	turnQueueCap  = 32              // pending worker ops (message.send / session.new)
)

// resumeErrHints are case-insensitive substrings in stderr/stdout meaning the
// resumed thread/session no longer exists (so a fresh retry is warranted).
// "no rollout found" is codex rust-v0.142.5's verbatim missing-thread error
// ("thread/resume failed: no rollout found for thread id <id>").
var resumeErrHints = []string{"no rollout found", "no conversation", "not found", "session"}

// Config holds every tunable. Main() fills it from environment variables
// (read once at start); tests construct it directly with temp paths.
type Config struct {
	Token       string        // CODEX_WS_TOKEN
	Port        string        // CODEX_PORT (Main only; tests inject a Listener)
	Workspace   string        // CODEX_WORKSPACE
	CodexBin    string        // CODEX_BIN
	CodexHome   string        // CODEX_HOME
	SessionFile string        // CODEX_SESSION_FILE
	AttachDir   string        // CODEX_ATTACH_DIR
	TurnTimeout time.Duration // CODEX_TURN_TIMEOUT_S
	Home        string        // HOME asserted into the subprocess env
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func configFromEnv() Config {
	timeout := 600 * time.Second
	if f, err := strconv.ParseFloat(envOr("CODEX_TURN_TIMEOUT_S", "600"), 64); err == nil && f > 0 {
		timeout = time.Duration(f * float64(time.Second))
	}
	return Config{
		Token:       envOr("CODEX_WS_TOKEN", "autonomous_codex_token"),
		Port:        envOr("CODEX_PORT", "18792"),
		Workspace:   envOr("CODEX_WORKSPACE", "/root/.codex/workspace"),
		CodexBin:    envOr("CODEX_BIN", "codex"),
		CodexHome:   envOr("CODEX_HOME", "/root/.codex"),
		SessionFile: envOr("CODEX_SESSION_FILE", "/root/.codex/session.json"),
		AttachDir:   envOr("CODEX_ATTACH_DIR", "/root/.codex/attachments"),
		TurnTimeout: timeout,
		Home:        "/root",
	}
}

// Server bridges a single WebSocket client to per-turn codex subprocesses.
type Server struct {
	cfg Config
	ln  net.Listener

	mu       sync.Mutex // guards client, threadID and session-file writes
	client   *wsClient  // single client; a new connection replaces the old
	threadID string     // current codex thread id ("" = fresh next turn)

	ops chan op // turns + session.new, strictly serialized by the single worker
}

// New builds a Server with explicit config and listener (tests use port 0).
func New(cfg Config, ln net.Listener) *Server {
	return &Server{
		cfg: cfg,
		ln:  ln,
		ops: make(chan op, turnQueueCap),
	}
}

// Serve blocks until ctx is cancelled or the listener fails. It owns the
// turn-worker goroutine; on ctx cancellation any in-flight subprocess is
// killed (process group) and open connections are dropped.
func (s *Server) Serve(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	if err := os.MkdirAll(s.cfg.Workspace, 0o755); err != nil {
		log.Printf("%s mkdir workspace failed: %v", logPrefix, err)
	}
	if err := os.MkdirAll(s.cfg.AttachDir, 0o755); err != nil {
		log.Printf("%s mkdir attach dir failed: %v", logPrefix, err)
	}
	s.threadID = s.loadSession()

	go s.turnWorker(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/codex/ws", s.handleWS)
	mux.HandleFunc("/codex/ws/", s.handleWS)
	httpSrv := &http.Server{Handler: mux}

	errCh := make(chan error, 1)
	go func() { errCh <- httpSrv.Serve(s.ln) }()
	log.Printf("%s listening on ws://%s/codex/ws/", logPrefix, s.ln.Addr())

	select {
	case <-ctx.Done():
		_ = httpSrv.Close()
		<-errCh
		return nil
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}

// Main is the blocking entry point for `os-server codex-gatewayd`. It reads
// config from the environment, listens on 127.0.0.1:CODEX_PORT and shuts
// down gracefully on SIGTERM/SIGINT.
func Main() int {
	cfg := configFromEnv()
	ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", cfg.Port))
	if err != nil {
		log.Printf("%s listen failed: %v", logPrefix, err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := New(cfg, ln).Serve(ctx); err != nil {
		log.Printf("%s serve failed: %v", logPrefix, err)
		return 1
	}
	log.Printf("%s shut down", logPrefix)
	return 0
}
