package intern

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"go.autonomous.ai/os/system/lib/internbridge"
)

// CanonicalAuthorityURL is the authority pinned by fleet_dispatch's URL validator.
const CanonicalAuthorityURL = "http://100.115.27.81:7370"

var ErrDispatch = errors.New("intern: authority dispatch failed; remote outcome unknown")
var ErrDispatchConfig = errors.New("intern: invalid dispatch configuration")

// ServiceDispatcher belongs to the Intern service, never the bridge or persona.
type ServiceDispatcher interface {
	Dispatch(context.Context, internbridge.Request, internbridge.Result) (*DispatchReceipt, error)
}

type DispatchReceipt struct {
	RunID       string `json:"run_id"`
	BridgeRunID string `json:"bridge_run_id"`
	TaskID      string `json:"task_id"`
	Destination string `json:"destination"`
	Status      string `json:"status"`
	Delivered   bool   `json:"delivered"`
	MessageID   string `json:"message_id"`
	Idempotent  bool   `json:"idempotent"`
}

// TokenSource is trusted runtime configuration, never request/model input. Its
// errors and values are never returned, logged, or included in a receipt.
type DispatchConfig struct {
	Endpoint    string
	Principal   string
	TokenSource func(context.Context) (string, error)
}

type substrateDispatcher struct {
	endpoint    string
	principal   string
	tokenSource func(context.Context) (string, error)
	http        *http.Client
}

// NewServiceDispatcher does not resolve credentials or perform network I/O.
// Tests inject an HTTP transport while retaining the canonical URL validation.
func NewServiceDispatcher(cfg DispatchConfig) (ServiceDispatcher, error) {
	if cfg.TokenSource == nil {
		return nil, nil
	}
	if cfg.Endpoint == "" {
		cfg.Endpoint = CanonicalAuthorityURL
	}
	if cfg.Endpoint != CanonicalAuthorityURL || cfg.Principal != "intern@gus" {
		return nil, ErrDispatchConfig
	}
	transport := &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: internbridge.RequestTimeout}).DialContext,
		DisableCompression: true, MaxResponseHeaderBytes: 8192, ResponseHeaderTimeout: internbridge.RequestTimeout,
		DisableKeepAlives: true}
	return &substrateDispatcher{endpoint: cfg.Endpoint, principal: cfg.Principal, tokenSource: cfg.TokenSource,
		http: &http.Client{Transport: transport, Timeout: internbridge.RequestTimeout,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func dispatcherFromEnvironment() (ServiceDispatcher, error) {
	// No fallback to provider keys or another fleet client's credential.
	if os.Getenv("GUS_INTERN_DISPATCH_TOKEN") == "" {
		return nil, nil
	}
	return NewServiceDispatcher(DispatchConfig{Endpoint: os.Getenv("GUS_INTERN_DISPATCH_URL"), Principal: os.Getenv("GUS_INTERN_DISPATCH_PRINCIPAL"),
		TokenSource: func(context.Context) (string, error) { return os.Getenv("GUS_INTERN_DISPATCH_TOKEN"), nil }})
}

var dispatchRunID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
var bearerToken = regexp.MustCompile(`^[A-Za-z0-9._~+/-]+=*$`)

func (d *substrateDispatcher) Dispatch(ctx context.Context, request internbridge.Request, route internbridge.Result) (*DispatchReceipt, error) {
	if route.RequestedDestination != "mcavoy@lab" && route.RequestedDestination != "pam@gus" {
		return nil, internbridge.ErrCustodyHold
	}
	if err := internbridge.ValidateServiceDispatch(request); err != nil {
		return nil, err
	}
	intent := route.ReceptionRoute.Intent
	if !((route.RequestedDestination == "mcavoy@lab" && (intent == "news" || intent == "briefing")) ||
		(route.RequestedDestination == "pam@gus" && (intent == "notification" || intent == "alarm" || intent == "reminder"))) {
		return nil, internbridge.ErrProtocol
	}
	if len(request.Text) > 2000 || strings.TrimSpace(request.Text) != request.Text ||
		strings.IndexFunc(request.Text, func(r rune) bool { return !unicode.IsPrint(r) }) >= 0 {
		return nil, internbridge.ErrInvalidRequest
	}
	hash := sha256.Sum256([]byte(request.RunID))
	if !dispatchRunID.MatchString(request.RunID) || route.RunID != "run-"+hex.EncodeToString(hash[:12]) || route.Kind != "service" || route.Status != "service_route" || route.Destination != internbridge.FirstContact || route.ReceptionRoute.Executed || route.ReceptionRoute.Handoff != route.RequestedDestination {
		return nil, internbridge.ErrProtocol
	}
	if ctx == nil {
		return nil, internbridge.ErrInvalidRequest
	}
	if ctx.Err() != nil {
		return nil, internbridge.ErrCanceled
	}
	token, err := d.tokenSource(ctx)
	if err != nil || token == "" {
		return nil, internbridge.ErrServiceRoute
	}
	if len(token) > 8192 || !bearerToken.MatchString(token) {
		return nil, ErrDispatchConfig
	}
	// The only task content sent is the admitted input, never model output,
	// thinking, history, persona memory, or an authority response body.
	payload := map[string]any{
		"request_id": request.RunID, "role": d.principal, "node": "gus", "requested_at": time.Now().Unix(),
		"to_role": route.RequestedDestination,
		"task": map[string]any{
			"schema_version": "gus-bus-task/v1", "task_id": request.RunID,
			"data_zone": request.DataClass, "custody_policy": "business-public-only", "instruction_inert": true,
			"body": map[string]string{"destination": route.RequestedDestination, "service_intent": intent,
				"request_text": request.Text, "idempotency_key": request.RunID},
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, internbridge.ErrInvalidRequest
	}
	if bytes.Contains(body, []byte(token)) || strings.Contains(request.Text, token) {
		return nil, internbridge.ErrCustodyHold
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, d.endpoint+"/messages/post-task", bytes.NewReader(body))
	if err != nil {
		return nil, ErrDispatchConfig
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-GUS-Principal", d.principal)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	resp, err := d.http.Do(req)
	if err != nil {
		return nil, ErrDispatch
	}
	defer resp.Body.Close()
	media, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || media != "application/json" || resp.Header.Get("Content-Encoding") != "" || (resp.StatusCode != 200 && resp.StatusCode != 202) {
		return nil, ErrDispatch
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, internbridge.MaxResponseBytes+1))
	if err != nil || len(raw) > internbridge.MaxResponseBytes || !utf8.Valid(raw) {
		return nil, ErrDispatch
	}
	// Reject duplicate keys and trailing JSON; do not retain authority content.
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if tok, err := decoder.Token(); err != nil || tok != json.Delim('{') {
		return nil, ErrDispatch
	}
	fields := map[string]json.RawMessage{}
	seen := map[string]bool{}
	for decoder.More() {
		tok, err := decoder.Token()
		key, ok := tok.(string)
		if err != nil || !ok {
			return nil, ErrDispatch
		}
		if seen[key] {
			return nil, ErrDispatch
		}
		seen[key] = true
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return nil, ErrDispatch
		}
		switch key {
		case "ok", "status", "task_id", "delivered", "contract_version", "message_id", "idempotent", "destination":
			fields[key] = value
		default:
			return nil, ErrDispatch
		}
	}
	if _, err := decoder.Token(); err != nil {
		return nil, ErrDispatch
	}
	if decoder.Decode(new(any)) != io.EOF {
		return nil, ErrDispatch
	}
	var ok bool
	var status, taskID string
	if json.Unmarshal(fields["ok"], &ok) != nil || !ok || json.Unmarshal(fields["status"], &status) != nil || json.Unmarshal(fields["task_id"], &taskID) != nil || taskID != request.RunID {
		return nil, ErrDispatch
	}
	if status != "accepted" && status != "queued" {
		return nil, ErrDispatch
	}
	var version, destination, messageID string
	var idempotent bool
	messageHash := sha256.Sum256([]byte("bus-message\x00" + d.principal + "\x00" + request.RunID))
	if json.Unmarshal(fields["contract_version"], &version) != nil || version != "gus.comms/v1" ||
		json.Unmarshal(fields["destination"], &destination) != nil || destination != route.RequestedDestination ||
		json.Unmarshal(fields["message_id"], &messageID) != nil || messageID != hex.EncodeToString(messageHash[:8]) ||
		(string(fields["idempotent"]) != "true" && string(fields["idempotent"]) != "false") ||
		json.Unmarshal(fields["idempotent"], &idempotent) != nil {
		return nil, ErrDispatch
	}
	if delivered, exists := fields["delivered"]; exists && string(delivered) != "false" {
		return nil, ErrDispatch
	}
	return &DispatchReceipt{RunID: request.RunID, BridgeRunID: route.RunID, TaskID: request.RunID, Destination: route.RequestedDestination, Status: status, Delivered: false, MessageID: messageID, Idempotent: idempotent}, nil
}
