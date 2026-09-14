package harness

import (
	"context"
	"errors"
	"regexp"
	"sync"
	"testing"
)

type voiceFake struct {
	mu             sync.Mutex
	status         Status
	frames         []Frame
	question       any
	mutationError  error
	receiptState   string
	beforeMutation func()
	beforeStatus   func()
}

func (f *voiceFake) Status() Status { f.mu.Lock(); defer f.mu.Unlock(); return f.status }
func (f *voiceFake) Request(_ context.Context, r Frame) (Frame, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.frames = append(f.frames, r)
	switch r["type"] {
	case "status":
		if f.beforeStatus != nil {
			f.beforeStatus()
		}
		return Frame{"openQuestion": f.question}, nil
	case "receipt.get":
		return Frame{"receipt": map[string]any{"state": f.receiptState}}, nil
	case "turn.send", "question.answer":
		// Match the owner CLI contract, not just the local Go transport.
		if key, _ := r["idempotencyKey"].(string); !regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`).MatchString(key) {
			return Frame{"error": map[string]any{"code": "INVALID_REQUEST", "message": "Invalid idempotencyKey"}}, nil
		}
		if f.beforeMutation != nil {
			f.beforeMutation()
		}
		if f.mutationError != nil {
			return nil, f.mutationError
		}
		return Frame{"receipt": map[string]any{"state": f.receiptState}}, nil
	}
	return Frame{}, nil
}
func newVoiceFake() *voiceFake {
	return &voiceFake{status: Status{Paired: true, Connected: true, MachineID: "computer"}, receiptState: "queued"}
}
func enableVoice(t *testing.T, f *voiceFake, cb VoiceCallbacks) *VoiceController {
	t.Helper()
	v := NewVoiceController(f, cb)
	if _, e := v.SetMode(context.Background(), true, "agent"); e != nil {
		t.Fatal(e)
	}
	f.frames = nil
	return v
}
func mutationCount(f *voiceFake) int {
	n := 0
	for _, r := range f.frames {
		if mutation(stringField(r, "type")) {
			n++
		}
	}
	return n
}
func questionSet(id string, keys ...string) any {
	rows := []VoiceQuestion{}
	for _, key := range keys {
		rows = append(rows, VoiceQuestion{Key: key, Q: key, Options: []string{"A", "B"}})
	}
	return voiceQuestionSet{RequestID: id, Questions: rows}
}
func TestVoiceDefaultRestartAndStale(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := NewVoiceController(f, VoiceCallbacks{})
	old := v.State()
	if old.Enabled {
		t.Fatal("enabled by default")
	}
	if e := v.Submit(ctx, "hello", "off", old.Generation); e == nil {
		t.Fatal("off accepted")
	}
	if len(f.frames) != 0 {
		t.Fatal("off sent")
	}
	s, e := v.SetMode(ctx, true, "agent")
	if e != nil {
		t.Fatal(e)
	}
	if e = v.Submit(ctx, "hello", "stale", old.Generation); e == nil {
		t.Fatal("stale accepted")
	}
	if mutationCount(f) != 0 {
		t.Fatal("stale mutation")
	}
	restart := NewVoiceController(f, VoiceCallbacks{}).State()
	if restart.Enabled || restart.Generation == s.Generation {
		t.Fatalf("bad restart: %+v", restart)
	}
}
func TestVoiceCallbackBeforeMutationAndDuplicate(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	registered := false
	f.beforeMutation = func() {
		if !registered {
			t.Error("mutation before callback")
		}
	}
	v := enableVoice(t, f, VoiceCallbacks{OnDispatch: func(a, r string) { registered = a == "agent" && r == "run" }})
	for i := 0; i < 2; i++ {
		if e := v.Submit(ctx, "hello", "run", v.State().Generation); e != nil {
			t.Fatal(e)
		}
	}
	if mutationCount(f) != 1 {
		t.Fatal("duplicate mutation")
	}
}
func TestVoiceUnknownReceiptBlocksThenSendsNewInput(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.mutationError = &DeliveryUnknownError{Cause: errors.New("timeout")}
	if e := v.Submit(ctx, "first", "first", v.State().Generation); e == nil {
		t.Fatal("unknown accepted")
	}
	pending := v.State().Pending
	if pending == nil {
		t.Fatal("missing pending")
	}
	f.receiptState = "unknown"
	if e := v.Submit(ctx, "second", "second", v.State().Generation); e == nil {
		t.Fatal("pending accepted")
	}
	if mutationCount(f) != 1 {
		t.Fatal("resent")
	}
	f.receiptState = "completed"
	f.mutationError = nil
	if e := v.Submit(ctx, "third", "third", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	if mutationCount(f) != 2 {
		t.Fatal("new input lost after receipt")
	}
	if f.frames[len(f.frames)-1]["text"] != "third" {
		t.Fatal("wrong input sent")
	}
}
func TestVoiceResolveDoesNotRetryAndOffPreservesPending(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.receiptState = "unknown"
	_ = v.Submit(ctx, "first", "first", v.State().Generation)
	p := v.State().Pending
	if p == nil {
		t.Fatal("missing pending")
	}
	_, _ = v.SetMode(ctx, false, "")
	if v.State().Pending == nil {
		t.Fatal("off discarded pending")
	}
	if e := v.Resolve("do_not_retry", "wrong"); e == nil {
		t.Fatal("wrong key accepted")
	}
	if e := v.Resolve("do_not_retry", p.IdempotencyKey); e != nil {
		t.Fatal(e)
	}
	if v.State().Pending != nil || mutationCount(f) != 1 {
		t.Fatal("resolve retried")
	}
}
func TestVoiceCollectQuestionsAndExactKeys(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	var prompt string
	v := enableVoice(t, f, VoiceCallbacks{OnResponse: func(_, _, s string) { prompt = s }})
	f.question = questionSet("q1", "first key", "second key")
	if e := v.Submit(ctx, "A", "one", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	if prompt == "" || mutationCount(f) != 0 {
		t.Fatal("did not collect")
	}
	if e := v.Submit(ctx, "free text", "two", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	last := f.frames[len(f.frames)-1]
	answers := last["answers"].(map[string]string)
	if last["type"] != "question.answer" || last["questionRequestId"] != "q1" || answers["first key"] != "A" || answers["second key"] != "free text" {
		t.Fatalf("bad answers: %#v", last)
	}
}
func TestVoiceQuestionReplacementDoesNotConsumeAnswer(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.question = questionSet("q1", "first", "second")
	if e := v.Submit(ctx, "A", "one", v.State().Generation); e != nil {
		t.Fatal(e)
	}
	f.question = questionSet("q2", "replacement")
	if e := v.Submit(ctx, "B", "two", v.State().Generation); e == nil {
		t.Fatal("replaced question accepted")
	}
	if mutationCount(f) != 0 {
		t.Fatal("replacement answered")
	}
	if e := v.Answer(ctx, "q1", map[string]string{"first": "A"}, "ui-stale"); e == nil {
		t.Fatal("stale UI answer")
	}
	if e := v.Answer(ctx, "q2", map[string]string{"wrong": "A"}, "ui-keys"); e == nil {
		t.Fatal("wrong keys accepted")
	}
}
func TestVoiceOfflineAndMachineChange(t *testing.T) {
	for _, offline := range []bool{true, false} {
		f := newVoiceFake()
		v := enableVoice(t, f, VoiceCallbacks{})
		if offline {
			f.status.Connected = false
		} else {
			f.status.MachineID = "other"
		}
		if e := v.Submit(context.Background(), "hello", "run", v.State().Generation); e == nil {
			t.Fatal("unavailable accepted")
		}
		if mutationCount(f) != 0 {
			t.Fatal("unavailable sent")
		}
	}
}

func TestVoiceSelectionWhileOffAndUnknownType(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	v := NewVoiceController(f, VoiceCallbacks{})
	s, e := v.SetMode(ctx, false, "agent")
	if e != nil || s.Enabled || s.AgentID != "agent" {
		t.Fatalf("selection: %+v %v", s, e)
	}
	s, e = v.SetMode(ctx, true, "agent")
	if e != nil {
		t.Fatal(e)
	}
	f.receiptState = "unknown"
	e = v.Submit(ctx, "hello", "run", s.Generation)
	var unknown *DeliveryUnknownError
	if !errors.As(e, &unknown) {
		t.Fatalf("unknown result type: %v", e)
	}
}
func TestVoiceConcurrentDuplicateAndDisable(t *testing.T) {
	ctx := context.Background()
	f := newVoiceFake()
	entered := make(chan struct{})
	release := make(chan struct{})
	f.beforeMutation = func() { close(entered); <-release }
	v := enableVoice(t, f, VoiceCallbacks{})
	s := v.State()
	done := make(chan error, 1)
	go func() { done <- v.Submit(ctx, "hello", "run", s.Generation) }()
	<-entered
	if e := v.Submit(ctx, "hello", "run", s.Generation); e != nil {
		t.Fatalf("concurrent duplicate returned error: %v", e)
	}
	off, e := v.SetMode(ctx, false, "")
	if e != nil || off.Enabled {
		t.Fatal("disable blocked")
	}
	close(release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	if mutationCount(f) != 1 {
		t.Fatal("duplicate sent")
	}
}

func TestVoiceRejectedReceiptReturnsKnownError(t *testing.T) {
	f := newVoiceFake()
	v := enableVoice(t, f, VoiceCallbacks{})
	f.receiptState = "rejected"
	e := v.Submit(context.Background(), "hello", "run", v.State().Generation)
	var unknown *DeliveryUnknownError
	if e == nil || errors.As(e, &unknown) || v.State().Pending != nil {
		t.Fatalf("rejection not resolved: %v %+v", e, v.State())
	}
}

func TestVoiceDisableWinsOverInFlightEnable(t *testing.T) {
	f := newVoiceFake()
	entered, release := make(chan struct{}), make(chan struct{})
	f.beforeStatus = func() { close(entered); <-release }
	v := NewVoiceController(f, VoiceCallbacks{})
	done := make(chan error, 1)
	go func() {
		_, err := v.SetMode(context.Background(), true, "agent")
		done <- err
	}()
	<-entered
	if _, err := v.SetMode(context.Background(), false, ""); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err == nil || v.State().Enabled {
		t.Fatalf("late enable undid explicit disable: %v %+v", err, v.State())
	}
}
