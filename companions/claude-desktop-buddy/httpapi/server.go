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
	"strings"
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
	auth          Authenticator
	status        StatusProvider
	approvals     ApprovalService
	activity      ActivitySink
	codeApprovals CodeApprovalService
}

// New builds the delivery layer from its ports.
func New(port int, auth Authenticator, status StatusProvider, approvals ApprovalService, activity ActivitySink, codeApprovals CodeApprovalService) *Server {
	return &Server{
		port:          port,
		startAt:       time.Now(),
		auth:          auth,
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

	// Liveness — left open (no sensitive data) so the plugin's discovery
	// (mDNS + /24 /health sweep) can find the device before it has a token.
	mux.HandleFunc("GET /health", s.handleHealth)

	// Status — exposes device state + pending prompts. Only the on-device agent
	// (the OpenClaw `claude-buddy` skill, loopback) reads it, so it's
	// loopback-only rather than admin-token-gated.
	mux.HandleFunc("GET /status", s.loopbackOnly(s.handleStatus))

	// Claude Desktop path — voice-approval round-trip the on-device OpenClaw
	// agent uses to approve/deny a Desktop permission prompt (relayed over BLE).
	// Called from loopback by the agent, so loopback-only.
	mux.HandleFunc("POST /claude-desktop/approve", s.loopbackOnly(s.handleApprove))
	mux.HandleFunc("POST /claude-desktop/deny", s.loopbackOnly(s.handleDeny))

	// Claude Code path — activity pushed by the plugin (Mac → device over the
	// LAN), so gated by the admin-password Bearer token via s.guard.
	mux.HandleFunc("POST /claude-code/notify", s.guard(s.handleNotify))
	mux.HandleFunc("POST /claude-code/usage", s.guard(s.handleUsage))

	// Claude Code reverse approval — the plugin's permission hook long-polls
	// /approval-request (LAN → admin-token); the on-device agent resolves via
	// /approve|/deny (loopback-only). /pending lists what's awaiting an answer.
	mux.HandleFunc("POST /claude-code/approval-request", s.guard(s.handleApprovalRequest))
	mux.HandleFunc("POST /claude-code/approve", s.handleCodeApprove)
	mux.HandleFunc("POST /claude-code/deny", s.handleCodeDeny)
	mux.HandleFunc("GET /claude-code/pending", s.loopbackOnly(s.handleCodePending))

	return mux
}

// decodeJSON reads a size-capped JSON body into v.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) error {
	return json.NewDecoder(io.LimitReader(r.Body, maxBodyBytes)).Decode(v)
}

// guard wraps a LAN-facing handler with admin-password auth: the caller must
// present a valid `Authorization: Bearer <admin-password>` header. Without a
// configured Authenticator the daemon fails closed (401) — an unconfigured
// device must not silently accept anonymous LAN traffic.
func (s *Server) guard(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if s.auth == nil || !s.auth.Authorize(token) {
			fail(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		h(w, r)
	}
}

// loopbackOnly wraps a handler so only the local host (the on-device agent) can
// reach it. LAN callers get 403. This protects endpoints the device itself
// drives — status reads and the agent's approve/deny relays.
func (s *Server) loopbackOnly(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isLoopback(r) {
			fail(w, http.StatusForbidden, "loopback-only")
			return
		}
		h(w, r)
	}
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
