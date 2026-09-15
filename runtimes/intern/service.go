// Package intern implements the externally owned, text-only Welcome Desk.
// It has no runtime, HAL, credentials, filesystem or channel dependencies.
package intern

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/internbridge"
)

const (
	BridgePort   uint16 = 8765
	QueueLimit          = 8
	ResultLimit         = 64
	ResultTTL           = 5 * time.Minute
	ReadinessTTL        = 30 * time.Second
)

var (
	ErrNotStarted     = errors.New("intern: local worker not started")
	ErrQueueFull      = errors.New("intern: queue or result capacity reached")
	ErrResultNotFound = errors.New("intern: result not found or expired")
)

// Admission contains exactly the text and classification approved by a trusted
// caller. No context, identity, attachments or history may be appended later.
// The zero value is invalid. This is a Go API trust boundary, not authentication.
type Admission struct {
	request internbridge.Request
	voice   context.Context
}

// AdmitTrustedVoiceRequest retains the authenticated grant lifetime across the
// queue. The caller must classify the exact transcript just as for typed text.
func AdmitTrustedVoiceRequest(ctx context.Context, r internbridge.Request) (Admission, error) {
	if ctx == nil || ctx.Err() != nil {
		return Admission{}, internbridge.ErrNeedsClassification
	}
	if _, bounded := ctx.Deadline(); !bounded {
		return Admission{}, internbridge.ErrNeedsClassification
	}
	a, err := AdmitTrustedRequest(r)
	a.voice = ctx
	return a, err
}

// AdmitTrustedRequest must only be called by a trusted classifier. The HTTP
// integration requires an authenticated administrator's explicit per-input
// assertion; sensing, channels and ordinary chat cannot mint admissions.
func AdmitTrustedRequest(r internbridge.Request) (Admission, error) {
	if r.RunID != "" {
		return Admission{}, internbridge.ErrInvalidRequest
	}
	if err := internbridge.ValidateRequest(r); err != nil {
		return Admission{}, err
	}
	return Admission{request: r}, nil
}

type Turn struct {
	RunID           string               `json:"run_id"`
	State           string               `json:"state"`
	Scope           string               `json:"scope"`
	ExecutesActions bool                 `json:"executes_actions"`
	Result          *internbridge.Result `json:"result,omitempty"`
	Error           string               `json:"error,omitempty"`
	// A local cancellation/deadline does not cancel remote bridge work.
	RemoteOutcomeUnknown bool `json:"remote_outcome_unknown,omitempty"`
}

type entry struct {
	turn    Turn
	expires time.Time
}
type job struct {
	id       string
	request  internbridge.Request
	deadline time.Time
	voice    context.Context
}

type Service struct {
	client      *internbridge.Client
	mu          sync.Mutex
	running     bool
	busy        bool
	manualBusy  bool
	connected   time.Time
	lastSuccess time.Time
	version     string
	queue       []job
	results     map[string]entry
	wake        chan struct{}
	started     chan struct{}
	// Local correlation annotations are bounded and never authorize delivery.
	traces map[string]string
	web    map[string]bool
	silent map[string]bool
}

var _ domain.AgentGateway = (*Service)(nil)

// New always owns only a client of the fixed loopback bridge. No URL, port,
// proxy, credential or alternative runtime can be selected through config.
func New() *Service {
	client, _ := internbridge.New(BridgePort) // nonzero compile-time port
	return newService(client)
}

func newService(client *internbridge.Client) *Service {
	return &Service{client: client, results: make(map[string]entry), wake: make(chan struct{}, 1), started: make(chan struct{}),
		traces: make(map[string]string), web: make(map[string]bool), silent: make(map[string]bool)}
}

func (*Service) Name() string      { return domain.AgentRuntimeIntern }
func (s *Service) Version() string { s.mu.Lock(); defer s.mu.Unlock(); return s.version }
func (s *Service) IsReady() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running && !s.lastSuccess.IsZero() && time.Since(s.lastSuccess) < ReadinessTTL
}
func (s *Service) ConnectedAt() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running || s.lastSuccess.IsZero() || time.Since(s.lastSuccess) >= ReadinessTTL {
		return 0
	}
	return s.connected.Unix()
}

// The bridge protocol reports no process uptime.
func (*Service) AgentUptime() int64 { return 0 }
func (s *Service) IsBusy() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.busy || s.manualBusy || len(s.queue) != 0
}
func (s *Service) SetBusy(b bool) {
	s.mu.Lock()
	s.manualBusy = b
	s.mu.Unlock()
	s.DrainPendingEvents()
}

// WaitStarted waits until the worker has entered its running state. The signal
// is per worker lifetime, so a later StartWS call gets a fresh signal after a
// prior worker has stopped. This does not change Submit: callers that do not
// wait still receive ErrNotStarted until StartWS has actually begun.
func (s *Service) WaitStarted(ctx context.Context) error {
	if s == nil {
		return ErrNotStarted
	}
	if ctx == nil {
		ctx = context.Background()
	}
	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return nil
	}
	if s.started == nil {
		s.started = make(chan struct{})
	}
	started := s.started
	s.mu.Unlock()
	select {
	case <-started:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (s *Service) DrainPendingEvents() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

var sequence atomic.Uint64

func (*Service) NextChatRunID() (string, string) {
	id := fmt.Sprintf("intern-%x-%x", time.Now().UnixNano(), sequence.Add(1))
	return id, id
}

// Submit admits bounded asynchronous work. Completion is available through
// Result using the returned OS run ID; internbridge verifies the hashed wire ID.
// The deadline includes queue time. Full queues reject without network effects.
func (s *Service) Submit(a Admission) (string, error) {
	if a.voice != nil && a.voice.Err() != nil {
		return "", internbridge.ErrNeedsClassification
	}
	if err := internbridge.ValidateRequest(a.request); err != nil {
		return "", err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
	if !s.running {
		return "", ErrNotStarted
	}
	if len(s.queue) >= QueueLimit || len(s.results) >= ResultLimit {
		return "", ErrQueueFull
	}
	_, id := s.NextChatRunID()
	r := a.request
	r.RunID = id
	s.results[id] = entry{turn: Turn{RunID: id, State: "queued", Scope: "bridge_request"}}
	s.queue = append(s.queue, job{id: id, request: r, deadline: time.Now().Add(internbridge.RequestTimeout), voice: a.voice})
	s.DrainPendingEvents()
	return id, nil
}

func (s *Service) Result(id string) (Turn, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
	e, ok := s.results[id]
	if !ok {
		return Turn{}, ErrResultNotFound
	}
	t := e.turn
	if t.Result != nil {
		copy := *t.Result
		t.Result = &copy
	}
	return t, nil
}

func (s *Service) pruneLocked(now time.Time) {
	for id, e := range s.results {
		if !e.expires.IsZero() && !now.Before(e.expires) {
			delete(s.results, id)
			delete(s.traces, id)
			delete(s.web, id)
			delete(s.silent, id)
		}
	}
}

// StartWS runs the local HTTP queue worker. There is no WebSocket or device
// lifecycle in this protocol. Results are polled, never fed to the action/TTS
// event handler. Cancellation terminates queued work and the current HTTP wait;
// it does not claim the remote process stopped. A second worker is ignored.
func (s *Service) StartWS(ctx context.Context, _ domain.AgentEventHandler) {
	if ctx == nil || ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return
	}
	if s.started == nil {
		s.started = make(chan struct{})
	}
	s.running = true
	close(s.started)
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		for _, j := range s.queue {
			s.finishLocked(j.id, nil, internbridge.ErrCanceled, false)
		}
		s.queue = nil
		s.busy = false
		s.manualBusy = false
		s.running = false
		s.lastSuccess = time.Time{}
		s.connected = time.Time{}
		s.version = ""
		s.started = make(chan struct{})
		s.mu.Unlock()
		s.client.CloseIdleConnections()
	}()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.wake:
		case <-ticker.C:
		}
		if ctx.Err() != nil {
			return
		}
		s.mu.Lock()
		s.pruneLocked(time.Now())
		// Expire queued work even while manually paused.
		for len(s.queue) > 0 && !time.Now().Before(s.queue[0].deadline) {
			j := s.queue[0]
			s.queue = s.queue[1:]
			s.finishLocked(j.id, nil, internbridge.ErrDeadline, false)
		}
		if s.manualBusy || len(s.queue) == 0 {
			s.mu.Unlock()
			continue
		}
		j := s.queue[0]
		s.queue = s.queue[1:]
		s.busy = true
		e := s.results[j.id]
		e.turn.State = "running"
		s.results[j.id] = e
		s.mu.Unlock()
		callCtx, cancel := context.WithDeadline(ctx, j.deadline)
		stopVoice := func() bool { return false }
		if j.voice != nil {
			// Enforce expiry synchronously as well as propagating revocation.
			if deadline, ok := j.voice.Deadline(); ok && deadline.Before(j.deadline) {
				cancel()
				callCtx, cancel = context.WithDeadline(ctx, deadline)
			}
			stopVoice = context.AfterFunc(j.voice, cancel)
		}
		var result *internbridge.Result
		var err error
		sent := false
		if j.voice != nil && j.voice.Err() != nil {
			err = internbridge.ErrCanceled
		} else {
			sent = true
			result, err = s.client.Do(callCtx, j.request)
		}
		stopVoice()
		cancel()
		s.mu.Lock()
		s.finishLocked(j.id, result, err, sent)
		s.busy = false
		if err == nil {
			s.version = internbridge.Version
			if result.Status == "draft" {
				if s.lastSuccess.IsZero() || time.Since(s.lastSuccess) >= ReadinessTTL {
					s.connected = time.Now()
				}
				s.lastSuccess = time.Now()
			}
		} else {
			s.lastSuccess = time.Time{}
			s.connected = time.Time{}
			s.version = ""
		}
		s.mu.Unlock()
		s.DrainPendingEvents()
	}
}

func (s *Service) finishLocked(id string, result *internbridge.Result, err error, sent bool) {
	e := s.results[id]
	e.turn.State = "completed"
	e.turn.Result = result
	if err != nil {
		e.turn.State = "failed"
		e.turn.Error = err.Error()
		if errors.Is(err, internbridge.ErrCanceled) {
			e.turn.State = "canceled"
		}
		e.turn.RemoteOutcomeUnknown = sent && (errors.Is(err, internbridge.ErrCanceled) || errors.Is(err, internbridge.ErrDeadline) || errors.Is(err, internbridge.ErrTransport))
	}
	e.expires = time.Now().Add(ResultTTL)
	s.results[id] = e
}

// Legacy string APIs lack a trusted classification and cannot enqueue content.
func (*Service) SendChatMessage(string) (string, error) {
	return "", internbridge.ErrNeedsClassification
}
func (*Service) SendSystemChatMessage(string) (string, error) {
	return "", internbridge.ErrNeedsClassification
}
func (s *Service) SendChatMessageWithImages(msg string, images []string) (string, error) {
	if len(images) != 0 {
		return "", domain.ErrNotSupportedByRuntime
	}
	return s.SendChatMessage(msg)
}
func (s *Service) SendChatMessageWithRun(msg, _, _ string) (string, error) {
	return s.SendChatMessage(msg)
}
func (s *Service) SendChatMessageWithImagesAndRun(msg string, images []string, _, _ string) (string, error) {
	return s.SendChatMessageWithImages(msg, images)
}

// Annotations are local correlation state only, capped even for legacy callers.
func (s *Service) MarkWebChatRun(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.web) < ResultLimit {
		s.web[id] = true
	}
}
func (s *Service) IsWebChatRun(id string) bool { s.mu.Lock(); defer s.mu.Unlock(); return s.web[id] }
func (s *Service) ConsumeWebChatRun(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.web[id]
	delete(s.web, id)
	return v
}
func (s *Service) MarkSilentRun(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.silent) < ResultLimit {
		s.silent[id] = true
	}
}
func (s *Service) IsSilentRun(id string) bool { s.mu.Lock(); defer s.mu.Unlock(); return s.silent[id] }
func (s *Service) ConsumeSilentRun(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.silent[id]
	delete(s.silent, id)
	return v
}
func (s *Service) SetPendingChatTrace(id, msg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Only already-admitted text may enter local trace storage.
	if _, ok := s.results[id]; ok && len(s.traces) < ResultLimit && len(msg) <= internbridge.MaxRequestBytes {
		s.traces[id] = msg
	}
}
func (s *Service) RemovePendingChatTraceByRunID(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.traces[id]
	delete(s.traces, id)
	return ok
}
func (s *Service) MatchPendingByMessage(msg string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	for id, text := range s.traces {
		if text == msg {
			delete(s.traces, id)
			return id
		}
	}
	return ""
}
func (s *Service) IsRecentOutboundChat(msg string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pruneLocked(time.Now())
	for _, text := range s.traces {
		if text == msg && strings.TrimSpace(msg) != "" {
			return true
		}
	}
	return false
}
