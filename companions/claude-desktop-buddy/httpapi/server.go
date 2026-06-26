// Package httpapi is the HTTP delivery layer for the buddy daemon. Handlers
// depend only on the small port interfaces in ports.go; package main wires in
// the concrete implementations (dependency inversion). Adding an endpoint means
// a line in routes() plus a handler in the matching file — the delivery layer
// never grows business logic.
package httpapi

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"time"
)

// maxBodyBytes caps decoded request bodies — these endpoints take small JSON
// from the LAN, so anything larger is malformed or hostile.
const maxBodyBytes = 64 << 10

// Server owns routing and response encoding and delegates all behaviour to its
// injected ports.
type Server struct {
	port          int
	startAt       time.Time
	status        StatusProvider
	approvals     ApprovalService
	activity      ActivitySink
	codeApprovals CodeApprovalService
}

// New builds the delivery layer from its ports.
func New(port int, status StatusProvider, approvals ApprovalService, activity ActivitySink, codeApprovals CodeApprovalService) *Server {
	return &Server{
		port:          port,
		startAt:       time.Now(),
		status:        status,
		approvals:     approvals,
		activity:      activity,
		codeApprovals: codeApprovals,
	}
}

// Start serves HTTP on the configured port.
func (s *Server) Start() error {
	addr := fmt.Sprintf(":%d", s.port)
	log.Printf("[http] listening on %s", addr)
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.routes(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       15 * time.Second,
		// WriteTimeout is intentionally 0 (disabled): /claude-code/approval-request
		// long-polls for up to ~55s, and a WriteTimeout would cut the held response.
		// The long-poll is bounded by the use case's own ttl + the request context.
		WriteTimeout: 0,
		IdleTimeout:  60 * time.Second,
	}
	return srv.ListenAndServe()
}

// routes is the single registry of endpoints. Device-internal endpoints live at
// the root; the Claude Code Buddy plugin pushes are namespaced under
// /claude-code/ so the two API surfaces stay clearly separated.
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()

	// Status / liveness — on-device agent + plugin discovery (shared).
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("GET /health", s.handleHealth)

	// Claude Desktop path — voice-approval round-trip the OpenClaw agent uses
	// to approve/deny a Desktop permission prompt (relayed back over BLE).
	mux.HandleFunc("POST /claude-desktop/approve", s.handleApprove)
	mux.HandleFunc("POST /claude-desktop/deny", s.handleDeny)

	// Claude Code path — activity pushed by the plugin (Mac → device over HTTP).
	mux.HandleFunc("POST /claude-code/notify", s.handleNotify)
	mux.HandleFunc("POST /claude-code/usage", s.handleUsage)

	// Claude Code reverse approval — the plugin's permission hook long-polls
	// /approval-request; the on-device agent resolves via /approve|/deny
	// (loopback-only). /pending lists what's awaiting a voice answer.
	mux.HandleFunc("POST /claude-code/approval-request", s.handleApprovalRequest)
	mux.HandleFunc("POST /claude-code/approve", s.handleCodeApprove)
	mux.HandleFunc("POST /claude-code/deny", s.handleCodeDeny)
	mux.HandleFunc("GET /claude-code/pending", s.handleCodePending)

	return mux
}

// decodeJSON reads a size-capped JSON body into v.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) error {
	return json.NewDecoder(io.LimitReader(r.Body, maxBodyBytes)).Decode(v)
}

// isLoopback reports whether the request came from the local host. Used to gate
// the code approve/deny endpoints so only the on-device agent can let code run
// on the user's Mac.
func isLoopback(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// --- response helpers ---

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// ok / fail emit the uniform {"ok":bool,...} envelope used by every endpoint.
func ok(w http.ResponseWriter) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]interface{}{"ok": false, "error": msg})
}
