// Package httpapi is the HTTP delivery layer for the buddy daemon. Handlers
// depend only on the small port interfaces in ports.go; package main wires in
// the concrete implementations (dependency inversion). Adding an endpoint means
// a line in routes() plus a handler in the matching file — the delivery layer
// never grows business logic.
package httpapi

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
)

// Server owns routing and response encoding and delegates all behaviour to its
// injected ports.
type Server struct {
	port      int
	startAt   time.Time
	status    StatusProvider
	approvals ApprovalService
	activity  ActivitySink
}

// New builds the delivery layer from its ports.
func New(port int, status StatusProvider, approvals ApprovalService, activity ActivitySink) *Server {
	return &Server{
		port:      port,
		startAt:   time.Now(),
		status:    status,
		approvals: approvals,
		activity:  activity,
	}
}

// Start serves HTTP on the configured port.
func (s *Server) Start() error {
	addr := fmt.Sprintf(":%d", s.port)
	log.Printf("[http] listening on %s", addr)
	return http.ListenAndServe(addr, s.routes())
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

	return mux
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
