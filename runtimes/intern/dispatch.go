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
	if cfg.Endpoint != CanonicalAuthorityURL || (cfg.Principal != "fleet-dispatch@dru" && cfg.Principal != "fleet-dispatch@gus" && cfg.Principal != "fleet-dispatch@lab") {
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

var dispatchRunID = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)
var bearerToken = regexp.MustCompile(`^[A-Za-z0-9._~+/-]+=*$`)

func (d *substrateDispatcher) Dispatch(ctx context.Context, request internbridge.Request, route internbridge.Result) (*DispatchReceipt, error) {
	if route.RequestedDestination != "mcavoy@lab" && route.RequestedDestination != "pam@gus" {
		return nil, internbridge.ErrCustodyHold
	}
	if err := internbridge.ValidateServiceDispatch(request); err != nil {
		return nil, err
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
	parts := strings.Split(route.RequestedDestination, "@")
	payload := map[string]any{
		"task_id": request.RunID, "request_id": request.RunID, "title": "intern service: " + route.RequestedDestination,
		"task": request.Text, "source": "intern", "classification": "PUBLIC", "dry_run": false,
		"requirements": map[string]any{"node": parts[1], "roles": []string{parts[0]}, "task_type": "service", "routing_hint_source": "intern-service",
			"timeout": 20, "max_tokens": 256, "temperature": 0.2},
	}
	if request.DataClass == internbridge.Business {
		payload["classification"] = "INTERNAL"
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, internbridge.ErrInvalidRequest
	}
	if bytes.Contains(body, []byte(token)) {
		return nil, internbridge.ErrCustodyHold
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, d.endpoint+"/dispatch", bytes.NewReader(body))
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
		// Keep only acknowledgement scalars during validation. Other content
		// is discarded and cannot enter service results, errors or receipts.
		if key == "ok" || key == "status" || key == "task_id" || key == "delivered" {
			fields[key] = value
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
	if delivered, exists := fields["delivered"]; exists && string(delivered) != "false" {
		return nil, ErrDispatch
	}
	return &DispatchReceipt{RunID: request.RunID, BridgeRunID: route.RunID, TaskID: request.RunID, Destination: route.RequestedDestination, Status: status, Delivered: false}, nil
}
