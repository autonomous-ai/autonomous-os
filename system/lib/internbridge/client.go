// Package internbridge is an unregistered client for Intern bridge protocol 0.2.0.
// It does not implement AgentGateway or provision a runtime. Data classification
// must come from trusted caller policy, never from the message or a model.
package internbridge

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
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	Version          = "0.2.0"
	ResponseSchema   = "cassi-first.v1"
	FirstContact     = "cassi@mama"
	MaxOutputChars   = 4096
	MaxRequestBytes  = 16 * 1024
	MaxResponseBytes = 64 * 1024
	RequestTimeout   = 20 * time.Second
)

var (
	ErrInvalidRequest      = errors.New("intern bridge: invalid request")
	ErrTransport           = errors.New("intern bridge: transport failed")
	ErrDeadline            = errors.New("intern bridge: deadline exceeded")
	ErrCanceled            = errors.New("intern bridge: canceled")
	ErrProtocol            = errors.New("intern bridge: invalid response")
	ErrResponseTooLarge    = errors.New("intern bridge: response too large")
	ErrRejected            = errors.New("intern bridge: request rejected")
	ErrUnavailable         = errors.New("intern bridge: inference unavailable")
	ErrCustodyHold         = errors.New("intern bridge: custody hold")
	ErrNeedsClassification = errors.New("intern bridge: trusted classification required")
	ErrNeedsInput          = errors.New("intern bridge: input required")
	ErrServiceRoute        = errors.New("intern bridge: service destination only")
)

// Error exposes only a fixed sentinel and an HTTP status, never server error
// text, request content, URLs, or underlying network/JSON error messages.
type Error struct {
	kind       error
	HTTPStatus int
}

func (e *Error) Error() string             { return e.kind.Error() }
func (e *Error) Unwrap() error             { return e.kind }
func failure(kind error, status int) error { return &Error{kind: kind, HTTPStatus: status} }

type Operation string

const (
	Route     Operation = "route"
	Classify  Operation = "classify"
	Generate  Operation = "generate"
	Reception Operation = "reception"
)

type DataClass string

const (
	Unknown    DataClass = "unknown"
	Public     DataClass = "public"
	Business   DataClass = "business"
	Restricted DataClass = "restricted"
	Secret     DataClass = "secret"
)

// Request requires an explicit Operation and DataClass. RunID is optional;
// caller-supplied IDs are opaque correlation tokens, not message content.
type Request struct {
	Text      string    `json:"text"`
	Operation Operation `json:"operation"`
	DataClass DataClass `json:"data_class"`
	RunID     string    `json:"run_id,omitempty"`
}

type Result struct {
	RunID string
	// Destination is always FirstContact, never an executor.
	Destination string
	// RequestedDestination and Kind describe a proposal after reception.
	RequestedDestination string
	ReceptionRoute       ReceptionRoute
	Kind                 string
	Status               string
	Output               string
}

// ReceptionRoute is metadata only. An empty Handoff represents wire null.
// No reception or handoff is executed by this client or claimed by this result.
type ReceptionRoute struct {
	FirstDestination string
	Handoff          string
	Intent           string
	Status           string
	Executed         bool
	NextStep         string
}

// Client is safe for concurrent use. Its sole destination is literal IPv4
// loopback; callers cannot inject a URL, proxy, transport, or credential.
type Client struct {
	base string
	http *http.Client
}

func New(port uint16) (*Client, error) {
	if port == 0 {
		return nil, failure(ErrInvalidRequest, 0)
	}
	transport := &http.Transport{
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: RequestTimeout}).DialContext,
		DisableCompression:     true,
		MaxResponseHeaderBytes: 8192,
		ResponseHeaderTimeout:  RequestTimeout,
		IdleConnTimeout:        30 * time.Second,
		MaxIdleConnsPerHost:    2,
		MaxConnsPerHost:        4,
	}
	return &Client{
		base: "http://127.0.0.1:" + strconv.Itoa(int(port)),
		http: &http.Client{Transport: transport, Timeout: RequestTimeout,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
	}, nil
}

func (c *Client) CloseIdleConnections() { c.http.CloseIdleConnections() }

var inputID = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)
var outputID = regexp.MustCompile(`^run-([a-f0-9]{24}|[a-f0-9]{32})$`)

func (r Request) valid() bool {
	if !utf8.ValidString(r.Text) || strings.TrimSpace(r.Text) == "" || utf8.RuneCountInString(r.Text) > 8000 {
		return false
	}
	if r.Operation != Route && r.Operation != Classify && r.Operation != Generate && r.Operation != Reception {
		return false
	}
	switch r.DataClass {
	case Unknown, Public, Business, Restricted, Secret:
	default:
		return false
	}
	return r.RunID == "" || inputID.MatchString(r.RunID)
}

// Do performs exactly one request. It never retries: the bridge reserves IDs
// before inference and has no result lookup API, so a timeout is ambiguous.
// Unknown, restricted, and secret data never leave this process, even for Route.
// Only reception_route (for Route/Reception), classified, and draft results succeed;
// completion covers the bridge request only, never an employee or service.
// Holds and fallback statuses return safe sentinels with no output attached.
func (c *Client) Do(ctx context.Context, request Request) (*Result, error) {
	if !request.valid() {
		return nil, failure(ErrInvalidRequest, 0)
	}
	body, err := json.Marshal(request)
	if err != nil || len(body) > MaxRequestBytes {
		return nil, failure(ErrInvalidRequest, 0)
	}
	// A loopback listener is a separate custody boundary, not trusted storage.
	// Do not send held content there merely to obtain a server-side rejection.
	switch request.DataClass {
	case Restricted, Secret:
		return nil, failure(ErrCustodyHold, 0)
	case Unknown:
		return nil, failure(ErrNeedsClassification, 0)
	}
	fields, status, err := c.call(ctx, "/v1/intern", body)
	if err != nil {
		return nil, err
	}
	if !exact(fields, "version", "response_schema", "run_id", "destination", "requested_destination", "reception_route", "kind", "status", "executes_actions", "transport_status", "lifecycle_status", "lifecycle_scope", "output") ||
		str(fields, "version") != Version || str(fields, "response_schema") != ResponseSchema || !isFalse(fields, "executes_actions") || str(fields, "lifecycle_scope") != "bridge_request" ||
		str(fields, "transport_status") != "accepted" || str(fields, "lifecycle_status") != "completed" {
		return nil, failure(ErrProtocol, status)
	}
	r := &Result{RunID: str(fields, "run_id"), Destination: str(fields, "destination"), RequestedDestination: str(fields, "requested_destination"), Kind: str(fields, "kind"), Status: str(fields, "status"), Output: str(fields, "output")}
	if !outputID.MatchString(r.RunID) {
		return nil, failure(ErrProtocol, status)
	}
	if request.RunID != "" {
		hash := sha256.Sum256([]byte(request.RunID))
		if r.RunID != "run-"+hex.EncodeToString(hash[:12]) {
			return nil, failure(ErrProtocol, status)
		}
	} else if len(r.RunID) != 36 {
		return nil, failure(ErrProtocol, status)
	}
	kinds := map[string]string{"orchestration@gus": "persona", "rex@dru": "persona", FirstContact: "persona", "melvil@lab": "persona", "pam@gus": "service", "mcavoy@lab": "service", "smart-home": "service"}
	if r.Destination != FirstContact || kinds[r.RequestedDestination] == "" || kinds[r.RequestedDestination] != r.Kind {
		return nil, failure(ErrProtocol, status)
	}
	if !r.validateReception(fields["reception_route"]) {
		return nil, failure(ErrProtocol, status)
	}
	if r.Status == "custody_hold" {
		if _, exists := fields["output"]; exists {
			return nil, failure(ErrProtocol, status)
		}
		if r.RequestedDestination != FirstContact && r.RequestedDestination != "smart-home" {
			return nil, failure(ErrProtocol, status)
		}
		return nil, failure(ErrCustodyHold, status)
	}
	if r.RequestedDestination == FirstContact || r.RequestedDestination == "smart-home" || strings.TrimSpace(r.Output) == "" || utf8.RuneCountInString(r.Output) > MaxOutputChars || strings.Contains(strings.ToLower(r.Output), "<think") || strings.Contains(strings.ToLower(r.Output), "[hw:") {
		return nil, failure(ErrProtocol, status)
	}
	if r.Kind == "service" {
		if r.Status != "service_route" {
			return nil, failure(ErrProtocol, status)
		}
		return nil, failure(ErrServiceRoute, status)
	}
	if request.Operation == Route || request.Operation == Reception && r.Status == "reception_route" {
		if r.Status != "reception_route" {
			return nil, failure(ErrProtocol, status)
		}
		return r, nil
	}
	if request.DataClass == Unknown {
		if r.Status != "needs_classification" {
			return nil, failure(ErrProtocol, status)
		}
		return nil, failure(ErrNeedsClassification, status)
	}
	switch r.Status {
	case "needs_input":
		return nil, failure(ErrNeedsInput, status)
	case "fallback":
		return nil, failure(ErrUnavailable, status)
	case "classified":
		if request.Operation != Classify {
			return nil, failure(ErrProtocol, status)
		}
		switch r.Output {
		case "question", "draft", "action", "unknown":
		default:
			return nil, failure(ErrProtocol, status)
		}
	case "draft":
		if request.Operation != Generate || strings.Contains(strings.ToLower(r.Output), "<think") || strings.Contains(strings.ToLower(r.Output), "[hw:") {
			return nil, failure(ErrProtocol, status)
		}
	default:
		return nil, failure(ErrProtocol, status)
	}
	return r, nil
}

func (r *Result) validateReception(raw []byte) bool {
	f, err := object(raw)
	if err != nil || len(f) != 6 || !exact(f, "first_destination", "handoff", "intent", "status", "executed", "next_step") ||
		str(f, "first_destination") != FirstContact || str(f, "status") != "reception_route" || !isFalse(f, "executed") || str(f, "next_step") != "safe_escalation" {
		return false
	}
	intent := str(f, "intent")
	switch intent {
	case "orchestration", "engineering", "service", "library", "reception", "smart-home", "news", "unknown", "address_or_default":
	default:
		return false
	}
	handoff := str(f, "handoff")
	if string(f["handoff"]) != "null" && (handoff == "" || handoff != r.RequestedDestination || handoff == FirstContact || r.Status == "custody_hold" || intent == "unknown") {
		return false
	}
	r.ReceptionRoute = ReceptionRoute{FirstDestination: FirstContact, Handoff: handoff, Intent: intent, Status: "reception_route", Executed: false, NextStep: "safe_escalation"}
	return true
}

// Health, Ready, and BridgeVersion validate transport metadata only. Ready
// does NOT prove model availability; protocol 0.2.0 never probes the model.
func (c *Client) Health(ctx context.Context) error { return c.probe(ctx, "/health", "ok") }
func (c *Client) Ready(ctx context.Context) error  { return c.probe(ctx, "/ready", "ready") }
func (c *Client) BridgeVersion(ctx context.Context) (string, error) {
	if err := c.probe(ctx, "/version", "ok"); err != nil {
		return "", err
	}
	return Version, nil
}
func (c *Client) probe(ctx context.Context, path, expected string) error {
	f, status, err := c.call(ctx, path, nil)
	if err != nil {
		return err
	}
	if len(f) != 5 || !exact(f, "version", "response_schema", "status", "transport", "executes_actions") || str(f, "version") != Version || str(f, "response_schema") != ResponseSchema || str(f, "status") != expected || str(f, "transport") != "loopback" || !isFalse(f, "executes_actions") {
		return failure(ErrProtocol, status)
	}
	return nil
}

func (c *Client) call(ctx context.Context, path string, body []byte) (map[string]json.RawMessage, int, error) {
	if ctx == nil {
		return nil, 0, failure(ErrInvalidRequest, 0)
	}
	ctx, cancel := context.WithTimeout(ctx, RequestTimeout)
	defer cancel()
	method := http.MethodGet
	if body != nil {
		method = http.MethodPost
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, bytes.NewReader(body))
	if err != nil {
		return nil, 0, failure(ErrInvalidRequest, 0)
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, networkError(ctx, err)
	}
	defer resp.Body.Close()
	status := resp.StatusCode
	if status >= 300 && status < 400 {
		return nil, status, failure(ErrProtocol, status)
	}
	media, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || media != "application/json" || resp.Header.Get("Content-Encoding") != "" {
		return nil, status, failure(ErrProtocol, status)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxResponseBytes+1))
	if err != nil {
		return nil, status, networkError(ctx, err)
	}
	if len(raw) > MaxResponseBytes {
		return nil, status, failure(ErrResponseTooLarge, status)
	}
	f, err := parseObject(raw, path == "/v1/intern" && status == http.StatusOK)
	if err != nil {
		return nil, status, failure(ErrProtocol, status)
	}
	if status != http.StatusOK {
		if !exact(f, "version", "response_schema", "error", "run_id", "destination", "requested_destination", "reception_route", "kind", "status", "executes_actions", "transport_status", "lifecycle_status", "lifecycle_scope") || str(f, "version") != Version || str(f, "error") == "" {
			return nil, status, failure(ErrProtocol, status)
		}
		// 404 is minimal; all other supported errors use the full null envelope.
		switch status {
		case 404:
			if len(f) != 2 {
				return nil, status, failure(ErrProtocol, status)
			}
		case 400, 409, 413, 500, 502:
			transport, lifecycle := "rejected", "not_started"
			if status == 500 || status == 502 {
				transport, lifecycle = "failed", "failed"
			}
			if len(f) != 13 || str(f, "response_schema") != ResponseSchema || str(f, "lifecycle_scope") != "bridge_request" || string(f["requested_destination"]) != "null" || string(f["reception_route"]) != "null" || str(f, "run_id") != "run-rejected" || string(f["destination"]) != "null" || string(f["kind"]) != "null" || str(f, "status") != "rejected" || !isFalse(f, "executes_actions") || str(f, "transport_status") != transport || str(f, "lifecycle_status") != lifecycle {
				return nil, status, failure(ErrProtocol, status)
			}
		default:
			return nil, status, failure(ErrProtocol, status)
		}
		if status == 502 {
			return nil, status, failure(ErrProtocol, status)
		}
		if status == 500 {
			return nil, status, failure(ErrUnavailable, status)
		}
		return nil, status, failure(ErrRejected, status)
	}
	return f, status, nil
}

func networkError(ctx context.Context, err error) error {
	if errors.Is(ctx.Err(), context.Canceled) {
		return failure(ErrCanceled, 0)
	}
	var timeout net.Error
	if errors.Is(ctx.Err(), context.DeadlineExceeded) || errors.As(err, &timeout) && timeout.Timeout() {
		return failure(ErrDeadline, 0)
	}
	return failure(ErrTransport, 0)
}

// object rejects duplicate keys, nested values, invalid UTF-8, and trailing JSON.
func object(raw []byte) (map[string]json.RawMessage, error) {
	return parseObject(raw, false)
}

// Only successful intern envelopes permit one nested reception_route object.
// Its exact scalar fields are validated separately; arrays remain forbidden.
func parseObject(raw []byte, reception bool) (map[string]json.RawMessage, error) {
	if !utf8.Valid(raw) {
		return nil, ErrProtocol
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	t, err := d.Token()
	if err != nil || t != json.Delim('{') {
		return nil, ErrProtocol
	}
	f := make(map[string]json.RawMessage)
	for d.More() {
		t, err := d.Token()
		if err != nil {
			return nil, ErrProtocol
		}
		key, ok := t.(string)
		if !ok {
			return nil, ErrProtocol
		}
		if _, ok := f[key]; ok {
			return nil, ErrProtocol
		}
		var v json.RawMessage
		if err := d.Decode(&v); err != nil {
			return nil, ErrProtocol
		}
		if len(v) == 0 || (v[0] == '{' && !(reception && key == "reception_route")) || v[0] == '[' || !validSurrogates(v) {
			return nil, ErrProtocol
		}
		f[key] = v
	}
	if _, err = d.Token(); err != nil {
		return nil, ErrProtocol
	}
	if _, err = d.Token(); err != io.EOF {
		return nil, ErrProtocol
	}
	return f, nil
}
func str(f map[string]json.RawMessage, key string) string {
	var s string
	if json.Unmarshal(f[key], &s) != nil {
		return ""
	}
	return s
}
func isFalse(f map[string]json.RawMessage, key string) bool { return string(f[key]) == "false" }

// exact permits only named fields. Required fields and types are checked by
// each envelope validator (output is deliberately absent on custody holds).
func exact(f map[string]json.RawMessage, keys ...string) bool {
	allowed := make(map[string]bool, len(keys))
	for _, k := range keys {
		allowed[k] = true
	}
	for k := range f {
		if !allowed[k] {
			return false
		}
	}
	return true
}

// encoding/json replaces unpaired UTF-16 surrogate escapes with U+FFFD.
// Reject them instead of silently changing bridge output; Python's valid
// paired escapes (including emoji) remain supported.
func validSurrogates(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return false
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		code, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			return false
		}
		i += 4
		if code >= 0xdc00 && code <= 0xdfff {
			return false
		}
		if code < 0xd800 || code > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
			return false
		}
		low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || low < 0xdc00 || low > 0xdfff {
			return false
		}
		i += 6
	}
	return true
}
