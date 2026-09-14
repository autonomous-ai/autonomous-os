package harness

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type VoiceTransport interface {
	Status() Status
	Request(context.Context, Frame) (Frame, error)
}
type VoiceCallbacks struct {
	OnDispatch func(agentID, runID string)
	OnResponse func(agentID, runID, text string)
}
type VoicePending struct {
	IdempotencyKey string `json:"idempotencyKey"`
	AgentID        string `json:"agentId"`
	MachineID      string `json:"machineId"`
	RunID          string `json:"runId"`
}
type VoiceModeState struct {
	Enabled    bool          `json:"enabled"`
	Generation uint64        `json:"generation"`
	MachineID  string        `json:"machineId"`
	AgentID    string        `json:"agentId"`
	Pending    *VoicePending `json:"pending,omitempty"`
	Error      string        `json:"error,omitempty"`
}
type VoiceQuestion struct {
	Key     string   `json:"key"`
	Q       string   `json:"q"`
	Options []string `json:"options"`
	Multi   bool     `json:"multi"`
}
type voiceQuestionSet struct {
	RequestID string          `json:"requestId"`
	Questions []VoiceQuestion `json:"questions"`
}
type VoiceController struct {
	mu        sync.Mutex
	op        sync.Mutex
	transport VoiceTransport
	callbacks VoiceCallbacks
	state     VoiceModeState
	seen      map[string]bool
	order     []string
	question  *voiceQuestionSet
	answers   map[string]string
}

var voiceGeneration atomic.Uint64

func nextVoiceGeneration() uint64 {
	seed := uint64(time.Now().UnixMicro())
	for {
		old := voiceGeneration.Load()
		if seed <= old {
			seed = old + 1
		}
		if voiceGeneration.CompareAndSwap(old, seed) {
			break
		}
	}
	return seed
}

func NewVoiceController(t VoiceTransport, cb VoiceCallbacks) *VoiceController {
	return &VoiceController{transport: t, callbacks: cb, state: VoiceModeState{Generation: nextVoiceGeneration()}, seen: make(map[string]bool)}
}
func (v *VoiceController) State() VoiceModeState {
	v.mu.Lock()
	defer v.mu.Unlock()
	s := v.state
	if s.Pending != nil {
		p := *s.Pending
		s.Pending = &p
	}
	return s
}
func (v *VoiceController) connected(machine string) error {
	s := v.transport.Status()
	if !s.Paired || !s.Connected {
		return errors.New("Harness is offline")
	}
	if machine != "" && s.MachineID != machine {
		return errors.New("Harness paired computer changed")
	}
	return nil
}
func voiceError(f Frame) error {
	if e, ok := f["error"]; ok && e != nil {
		return fmt.Errorf("Harness: %v", e)
	}
	return nil
}
func (v *VoiceController) request(ctx context.Context, f Frame) (Frame, error) {
	r, e := v.transport.Request(ctx, f)
	if e == nil {
		e = voiceError(r)
	}
	return r, e
}
func (v *VoiceController) Agents(ctx context.Context) (Frame, error) {
	if e := v.connected(""); e != nil {
		return nil, e
	}
	return v.request(ctx, Frame{"type": "agents.list"})
}
func (v *VoiceController) SetMode(ctx context.Context, enabled bool, agentID string) (VoiceModeState, error) {
	agentID = strings.TrimSpace(agentID)
	current := v.State()
	if !enabled && (agentID == "" || agentID == current.AgentID) {
		v.mu.Lock()
		// Also invalidate an enable operation that is still validating its
		// target while the visible state remains off.
		v.state.Generation = nextVoiceGeneration()
		v.state.Enabled = false
		v.question = nil
		v.answers = nil
		v.mu.Unlock()
		return v.State(), nil
	}
	if !v.op.TryLock() {
		return v.State(), errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	agentID = strings.TrimSpace(agentID)
	if agentID == "" {
		return v.State(), errors.New("Select a Harness agent")
	}
	before := v.State()
	if before.Pending != nil && (before.AgentID != agentID) {
		return before, errors.New("Resolve pending Harness delivery before changing agent")
	}
	if e := v.connected(""); e != nil {
		return v.State(), e
	}
	machine := v.transport.Status().MachineID
	if _, e := v.request(ctx, Frame{"type": "status", "machineId": machine, "agentId": agentID}); e != nil {
		return v.State(), e
	}
	v.mu.Lock()
	if v.state.Generation != before.Generation {
		v.mu.Unlock()
		return v.State(), errors.New("Harness voice mode changed")
	}
	if v.state.Pending != nil && v.state.Pending.MachineID != machine {
		v.mu.Unlock()
		return v.State(), errors.New("Resolve pending delivery from the previous computer")
	}
	if v.state.Enabled != enabled || v.state.AgentID != agentID || v.state.MachineID != machine {
		v.state.Generation = nextVoiceGeneration()
		v.question = nil
		v.answers = nil
	}
	v.state.Enabled = enabled
	v.state.AgentID = agentID
	v.state.MachineID = machine
	v.state.Error = ""
	v.mu.Unlock()
	return v.State(), nil
}
func (v *VoiceController) liveQuestion(ctx context.Context, s VoiceModeState) (*voiceQuestionSet, error) {
	if e := v.connected(s.MachineID); e != nil {
		return nil, e
	}
	f, e := v.request(ctx, Frame{"type": "status", "machineId": s.MachineID, "agentId": s.AgentID})
	if e != nil {
		return nil, e
	}
	if f["openQuestion"] == nil {
		return nil, nil
	}
	raw, e := json.Marshal(f["openQuestion"])
	if e != nil {
		return nil, e
	}
	var q voiceQuestionSet
	if e = json.Unmarshal(raw, &q); e != nil {
		return nil, fmt.Errorf("decode Harness question: %w", e)
	}
	if q.RequestID == "" || len(q.Questions) == 0 {
		return nil, errors.New("Harness question is malformed")
	}
	keys := map[string]bool{}
	for _, row := range q.Questions {
		if row.Key == "" || keys[row.Key] {
			return nil, errors.New("Harness question keys are malformed")
		}
		keys[row.Key] = true
	}
	return &q, nil
}
func (v *VoiceController) Question(ctx context.Context) (Frame, error) {
	s := v.State()
	if !s.Enabled {
		return Frame{"question": nil}, nil
	}
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return nil, e
	}
	if q == nil {
		return Frame{"question": nil}, nil
	}
	return Frame{"agentId": s.AgentID, "questionRequestId": q.RequestID, "questions": q.Questions}, nil
}
func (v *VoiceController) begin(runID string, generation uint64) (VoiceModeState, bool, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	s := v.state
	if strings.TrimSpace(runID) == "" {
		return s, false, errors.New("Harness voice run ID is required")
	}
	if v.seen[runID] {
		return s, true, nil
	}
	v.seen[runID] = true
	v.order = append(v.order, runID)
	if len(v.order) > 1024 {
		delete(v.seen, v.order[0])
		v.order = v.order[1:]
	}
	if !s.Enabled {
		return s, false, errors.New("Harness-only mode is off")
	}
	if generation != s.Generation {
		return s, false, errors.New("Harness voice mode changed; please repeat")
	}
	return s, false, nil
}
func (v *VoiceController) ready(ctx context.Context, s VoiceModeState) error {
	if e := v.connected(s.MachineID); e != nil {
		return e
	}
	if v.State().Pending != nil {
		if _, e := v.receipt(ctx); e != nil {
			return e
		}
		if v.State().Pending != nil {
			return errors.New("Previous Harness delivery is unresolved; inspect its receipt or resolve it without retrying")
		}
	}
	return nil
}
func (v *VoiceController) Submit(ctx context.Context, text, runID string, generation uint64) error {
	s, duplicate, e := v.begin(runID, generation)
	if duplicate || e != nil {
		return e
	}
	if !v.op.TryLock() {
		return errors.New("Harness voice operation is busy; please repeat")
	}
	defer v.op.Unlock()
	text = strings.TrimSpace(text)
	if text == "" || len(text) > 16384 {
		return errors.New("Voice text must contain 1 to 16384 UTF-8 bytes")
	}
	if e = v.ready(ctx, s); e != nil {
		return e
	}
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return e
	}
	v.mu.Lock()
	if v.state.Generation != s.Generation || !v.state.Enabled {
		v.mu.Unlock()
		return errors.New("Harness voice mode changed; please repeat")
	}
	if v.question != nil && !reflect.DeepEqual(v.question, q) {
		v.question = nil
		v.answers = nil
		v.mu.Unlock()
		return errors.New("Harness question changed; refresh the question and repeat your answer")
	}
	if q == nil {
		v.mu.Unlock()
		return v.dispatch(ctx, s, runID, Frame{"type": "turn.send", "text": text})
	}
	if v.question == nil {
		v.question = q
		v.answers = make(map[string]string)
	}
	for _, row := range q.Questions {
		if _, ok := v.answers[row.Key]; !ok {
			v.answers[row.Key] = text
			break
		}
	}
	var next *VoiceQuestion
	for i := range q.Questions {
		if _, ok := v.answers[q.Questions[i].Key]; !ok {
			next = &q.Questions[i]
			break
		}
	}
	answers := make(map[string]string, len(v.answers))
	for k, a := range v.answers {
		answers[k] = a
	}
	v.mu.Unlock()
	if next != nil {
		if v.callbacks.OnResponse != nil {
			prompt := next.Q
			if len(next.Options) > 0 {
				prompt += "\n" + strings.Join(next.Options, ", ")
			}
			v.callbacks.OnResponse(s.AgentID, runID, prompt)
		}
		return nil
	}
	return v.dispatch(ctx, s, runID, Frame{"type": "question.answer", "questionRequestId": q.RequestID, "answers": answers})
}
func (v *VoiceController) Answer(ctx context.Context, questionID string, answers map[string]string, runID string) error {
	s, duplicate, e := v.begin(runID, v.State().Generation)
	if duplicate || e != nil {
		return e
	}
	if !v.op.TryLock() {
		return errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	if e = v.ready(ctx, s); e != nil {
		return e
	}
	q, e := v.liveQuestion(ctx, s)
	if e != nil {
		return e
	}
	if q == nil || q.RequestID != questionID {
		return errors.New("Harness question is no longer open")
	}
	if len(answers) != len(q.Questions) {
		return errors.New("Answer every Harness question using its exact key")
	}
	copyAnswers := map[string]string{}
	for _, row := range q.Questions {
		a, ok := answers[row.Key]
		if !ok || strings.TrimSpace(a) == "" {
			return errors.New("Answer every Harness question using its exact key")
		}
		copyAnswers[row.Key] = a
	}
	return v.dispatch(ctx, s, runID, Frame{"type": "question.answer", "questionRequestId": questionID, "answers": copyAnswers})
}
func knownVoiceReceipt(f Frame) bool {
	raw, _ := json.Marshal(f["receipt"])
	var r struct {
		State string `json:"state"`
	}
	_ = json.Unmarshal(raw, &r)
	switch r.State {
	case "queued", "delivered", "started", "completed", "rejected":
		return true
	}
	return false
}
func (v *VoiceController) dispatch(ctx context.Context, s VoiceModeState, runID string, f Frame) error {
	if e := v.connected(s.MachineID); e != nil {
		return e
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d:%s:%s:%s", s.Generation, s.MachineID, s.AgentID, runID)))
	// CLI keys allow at most 64 characters. The 128-bit digest retains ample
	// collision resistance while including the readable operation prefix.
	key := "voice-" + hex.EncodeToString(sum[:16])
	p := &VoicePending{IdempotencyKey: key, AgentID: s.AgentID, MachineID: s.MachineID, RunID: runID}
	v.mu.Lock()
	if !v.state.Enabled || v.state.Generation != s.Generation {
		v.mu.Unlock()
		return errors.New("Harness voice mode changed; please repeat")
	}
	v.state.Pending = p
	v.question = nil
	v.answers = nil
	v.mu.Unlock()
	f["machineId"] = s.MachineID
	f["agentId"] = s.AgentID
	f["idempotencyKey"] = key
	if v.callbacks.OnDispatch != nil {
		v.callbacks.OnDispatch(s.AgentID, runID)
	}
	result, e := v.transport.Request(ctx, f)
	known := knownVoiceReceipt(result)
	if known && e == nil {
		raw, _ := json.Marshal(result["receipt"])
		var receipt struct {
			State string `json:"state"`
			Error any    `json:"error"`
		}
		_ = json.Unmarshal(raw, &receipt)
		if receipt.State == "rejected" {
			e = fmt.Errorf("Harness rejected the request: %v", receipt.Error)
		}
	}
	var unknown *DeliveryUnknownError
	// The transport marks every failure after sending as DeliveryUnknownError.
	if e != nil && !errors.As(e, &unknown) {
		known = true
	}
	if e == nil {
		e = voiceError(result)
		if e != nil {
			raw, _ := json.Marshal(result["error"])
			var remote struct {
				Code string `json:"code"`
			}
			_ = json.Unmarshal(raw, &remote)
			switch remote.Code {
			case "INVALID_REQUEST", "UNSUPPORTED_CAPABILITY", "MISSING_TARGET", "MACHINE_MISMATCH", "AGENT_NOT_FOUND", "QUESTION_STALE", "PAYLOAD_TOO_LARGE", "RATE_LIMITED", "BACKPRESSURE":
				known = true
			}
		}
	}
	v.mu.Lock()
	if known {
		v.state.Pending = nil
		v.state.Error = ""
	} else {
		v.state.Error = "Harness delivery is unconfirmed; inspect the receipt before sending again"
	}
	v.mu.Unlock()
	if !known {
		if e == nil {
			e = errors.New("Harness delivery is unconfirmed; inspect the receipt before sending again")
		}
		return &DeliveryUnknownError{RequestID: key, Cause: e}
	}
	if e != nil {
		return e
	}
	return nil
}
func (v *VoiceController) receipt(ctx context.Context) (Frame, error) {
	s := v.State()
	if s.Pending == nil {
		return Frame{"receipt": nil, "pending": false}, nil
	}
	if e := v.connected(s.Pending.MachineID); e != nil {
		return nil, e
	}
	f, e := v.request(ctx, Frame{"type": "receipt.get", "idempotencyKey": s.Pending.IdempotencyKey})
	if e != nil {
		return nil, e
	}
	if knownVoiceReceipt(f) {
		v.mu.Lock()
		if v.state.Pending != nil && v.state.Pending.IdempotencyKey == s.Pending.IdempotencyKey {
			v.state.Pending = nil
			v.state.Error = ""
		}
		v.mu.Unlock()
	}
	return f, nil
}
func (v *VoiceController) Receipt(ctx context.Context) (Frame, error) {
	if !v.op.TryLock() {
		return nil, errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	return v.receipt(ctx)
}
func (v *VoiceController) Resolve(resolution, key string) error {
	if !v.op.TryLock() {
		return errors.New("Harness voice operation is busy")
	}
	defer v.op.Unlock()
	v.mu.Lock()
	defer v.mu.Unlock()
	if resolution != "do_not_retry" || v.state.Pending == nil || v.state.Pending.IdempotencyKey != key {
		return errors.New("Resolve requires do_not_retry and the current pending idempotency key")
	}
	v.state.Pending = nil
	v.state.Error = ""
	return nil
}
