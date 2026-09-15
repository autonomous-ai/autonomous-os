// Package bridge owns the device-resident, text-only Intern HTTP listener.
// It has no process, hardware, channel or fallback-runtime dependency.
package bridge

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/lib/internbridge"
)

const Address = "127.0.0.1:8765"

// IDs are retained for this process lifetime. At capacity fail closed instead
// of evicting IDs and silently accepting replays. No request text is retained.
const maxRunIDs = 4096

var ErrListener = errors.New("intern bridge: listener unavailable")

type Server struct {
	http     *http.Server
	done     chan error
	provider Provider
	listener net.Listener
	cancel   context.CancelFunc
}

// Start binds synchronously and never adopts an existing listener. The caller
// owns Close and must observe Done; failures must stop the Intern OS lifecycle.
func Start(ctx context.Context, cfg ProviderConfig) (*Server, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, ErrListener
	}
	p, err := NewProvider(cfg)
	if err != nil {
		return nil, err
	}
	l, err := net.Listen("tcp4", Address)
	if err != nil {
		p.Close()
		return nil, ErrListener
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &Server{done: make(chan error, 1), provider: p, listener: l, cancel: cancel}
	s.http = &http.Server{
		Handler: newHandler(p), BaseContext: func(net.Listener) context.Context { return ctx },
		ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 5 * time.Second,
		WriteTimeout: 18 * time.Second, IdleTimeout: 15 * time.Second,
		MaxHeaderBytes: 8192, ErrorLog: log.New(io.Discard, "", 0),
	}
	go func() {
		err := s.http.Serve(l)
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			err = ErrListener
		} else {
			err = nil
		}
		s.done <- err
		close(s.done)
	}()
	return s, nil
}

func (s *Server) Done() <-chan error { return s.done }

// Close drains requests, then forcibly releases sockets at the deadline.
func (s *Server) Close(ctx context.Context) {
	s.cancel()
	if s.http.Shutdown(ctx) != nil {
		_ = s.http.Close()
	}
	// Shutdown can race Serve before net/http registers its listener. Explicit
	// ownership releases the already-bound socket even on startup failure.
	_ = s.listener.Close()
	<-s.done
	s.provider.Close()
}

type handler struct {
	mu       sync.Mutex
	ids      map[string]struct{}
	provider Provider
}

func newHandler(p Provider) *handler { return &handler{ids: make(map[string]struct{}), provider: p} }

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func reject(w http.ResponseWriter, status int) {
	if status == 404 {
		writeJSON(w, status, map[string]any{"version": internbridge.Version, "error": "not found"})
		return
	}
	transport, lifecycle := "rejected", "not_started"
	if status == 500 {
		transport, lifecycle = "failed", "failed"
	}
	writeJSON(w, status, map[string]any{
		"version": internbridge.Version, "response_schema": internbridge.ResponseSchema,
		"run_id": "run-rejected", "destination": nil, "requested_destination": nil,
		"reception_route": nil, "kind": nil, "status": "rejected", "executes_actions": false,
		"transport_status": transport, "lifecycle_status": lifecycle, "lifecycle_scope": "bridge_request",
		"error": "request unavailable or rejected",
	})
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Literal host and direct non-browser requests only: no DNS rebinding,
	// absolute-form proxy requests, forwarded admissions, queries or credentials.
	host, _, err := net.SplitHostPort(r.Host)
	if err != nil || host != "127.0.0.1" || r.URL.IsAbs() || r.URL.RawQuery != "" || r.URL.ForceQuery {
		reject(w, 400)
		return
	}
	for _, header := range []string{"Origin", "Authorization", "Proxy-Authorization", "Cookie", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "Content-Encoding"} {
		if len(r.Header.Values(header)) != 0 {
			reject(w, 400)
			return
		}
	}
	switch r.URL.Path {
	case "/health", "/ready", "/version":
		if r.Method != http.MethodGet {
			reject(w, 400)
			return
		}
		status := "ok"
		if r.URL.Path == "/ready" {
			status = "ready"
		}
		writeJSON(w, 200, map[string]any{"version": internbridge.Version, "response_schema": internbridge.ResponseSchema,
			"status": status, "transport": "loopback", "executes_actions": false})
	case "/v1/intern":
		if r.Method != http.MethodPost {
			reject(w, 400)
			return
		}
		media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || media != "application/json" {
			reject(w, 400)
			return
		}
		raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, internbridge.MaxRequestBytes))
		if err != nil {
			var size *http.MaxBytesError
			if errors.As(err, &size) {
				reject(w, 413)
			} else {
				reject(w, 400)
			}
			return
		}
		req, err := decodeRequest(raw)
		if err != nil {
			reject(w, 400)
			return
		}
		// Reuse the client's admission validator before routing or generation.
		err = internbridge.ValidateRequest(req)
		if err != nil && !errors.Is(err, internbridge.ErrCustodyHold) && !errors.Is(err, internbridge.ErrNeedsClassification) {
			reject(w, 400)
			return
		}
		id, status := h.reserve(req.RunID)
		if status != 0 {
			reject(w, status)
			return
		}
		var result response
		if errors.Is(err, internbridge.ErrCustodyHold) {
			result = envelope(id, internbridge.FirstContact, "persona", "custody_hold", "unknown", nil, nil)
		} else if errors.Is(err, internbridge.ErrNeedsClassification) {
			result = envelope(id, "orchestration@gus", "persona", "needs_classification", "unknown", nil, text("This request needs review before I can help."))
		} else {
			result = route(req, id)
			if result.Status == "fallback" && h.provider != nil {
				if output, err := h.provider.Complete(r.Context(), req); err == nil && safeOutput(output) {
					if req.Operation != internbridge.Classify || output == "question" || output == "draft" || output == "action" || output == "unknown" {
						result.Status = "draft"
						if req.Operation == internbridge.Classify {
							result.Status = "classified"
						}
						result.Output = text(output)
					}
				}
			}
		}
		writeJSON(w, 200, result)
	default:
		reject(w, 404)
	}
}

func (h *handler) reserve(input string) (string, int) {
	if input == "" {
		var nonce [16]byte
		if _, err := rand.Read(nonce[:]); err != nil {
			return "", 500
		}
		return "run-" + hex.EncodeToString(nonce[:]), 0
	}
	digest := sha256.Sum256([]byte(input))
	id := "run-" + hex.EncodeToString(digest[:12])
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.ids[id]; ok {
		return "", 409
	}
	if len(h.ids) >= maxRunIDs {
		return "", 500
	}
	h.ids[id] = struct{}{}
	return id, 0
}

func text(s string) *string { return &s }

type reception struct {
	FirstDestination string  `json:"first_destination"`
	Handoff          *string `json:"handoff"`
	Intent           string  `json:"intent"`
	Status           string  `json:"status"`
	Executed         bool    `json:"executed"`
	NextStep         string  `json:"next_step"`
}

type response struct {
	Version              string    `json:"version"`
	Schema               string    `json:"response_schema"`
	RunID                string    `json:"run_id"`
	Destination          string    `json:"destination"`
	RequestedDestination string    `json:"requested_destination"`
	Reception            reception `json:"reception_route"`
	Kind                 string    `json:"kind"`
	Status               string    `json:"status"`
	ExecutesActions      bool      `json:"executes_actions"`
	TransportStatus      string    `json:"transport_status"`
	LifecycleStatus      string    `json:"lifecycle_status"`
	LifecycleScope       string    `json:"lifecycle_scope"`
	Output               *string   `json:"output,omitempty"`
}

func envelope(id, destination, kind, status, intent string, handoff, output *string) response {
	return response{Version: internbridge.Version, Schema: internbridge.ResponseSchema, RunID: id,
		Destination: internbridge.FirstContact, RequestedDestination: destination, Kind: kind, Status: status,
		TransportStatus: "accepted", LifecycleStatus: "completed", LifecycleScope: "bridge_request", Output: output,
		Reception: reception{FirstDestination: internbridge.FirstContact, Handoff: handoff, Intent: intent, Status: "reception_route", NextStep: "safe_escalation"}}
}

// Match one company wake prefix and at most one explicit internal address.
// Names are proposals only. Natural language intent is never guessed by a model.
var wake = regexp.MustCompile(`(?i)^\s*(?:(?:hey|ok|okay)[\s_]+)?(gus|rex|cassi|casi|cassandra|melvil|curator|pam|news|smart[- ]home|orchestration|engineering|service|library|reception)(?:$|[\s,:!?.]+)`)

func address(s string) (string, string, bool) {
	m := wake.FindStringSubmatchIndex(s)
	if m == nil {
		return "gus", strings.TrimSpace(s), false
	}
	return strings.ToLower(s[m[2]:m[3]]), strings.TrimSpace(s[m[1]:]), true
}

func route(req internbridge.Request, id string) response {
	name, prompt, explicit := address(req.Text)
	if name == "gus" {
		if next, rest, ok := address(prompt); ok {
			name, prompt = next, rest
		}
	}
	destination, kind, intent := "orchestration@gus", "persona", "address_or_default"
	switch name {
	case "rex", "engineering":
		destination, intent = "rex@dru", "engineering"
	case "melvil", "curator", "library":
		destination, intent = "melvil@lab", "library"
	case "pam", "service":
		destination, kind, intent = "pam@gus", "service", "service"
	case "news":
		destination, kind, intent = "mcavoy@lab", "service", "news"
	case "smart-home", "smart home":
		destination, kind, intent = "smart-home", "service", "smart-home"
	case "cassi", "casi", "cassandra", "reception":
		destination, intent = internbridge.FirstContact, "reception"
	case "gus", "orchestration":
		if explicit {
			intent = "orchestration"
		}
	}
	if destination == internbridge.FirstContact || destination == "smart-home" {
		return envelope(id, destination, kind, "custody_hold", intent, nil, nil)
	}
	if kind == "service" {
		return envelope(id, destination, kind, "service_route", intent, text(destination), text("This request needs review."))
	}
	if req.Operation == internbridge.Route || req.Operation == internbridge.Reception {
		// Unaddressed reception needs intent review, never a guessed handoff.
		var handoff *string
		if explicit {
			handoff = text(destination)
		} else {
			intent = "unknown"
		}
		return envelope(id, destination, kind, "reception_route", intent, handoff, text("Welcome to GUSystems. I'm Cassi, the receptionist. How can I help?"))
	}
	if prompt == "" {
		return envelope(id, destination, kind, "needs_input", intent, nil, text("Provide a request."))
	}
	var handoff *string
	if explicit {
		handoff = text(destination)
	} else {
		intent = "unknown"
	}
	// The handler may replace this bounded fallback only with validated output.
	return envelope(id, destination, kind, "fallback", intent, handoff, text("I couldn’t answer that safely. Please try again."))
}
