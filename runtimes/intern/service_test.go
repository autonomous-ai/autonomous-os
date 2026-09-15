package intern

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/internbridge"
)

func bridgeFixture(t *testing.T, handler http.HandlerFunc) *Service {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	client, err := internbridge.New(uint16(server.Listener.Addr().(*net.TCPAddr).Port))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.CloseIdleConnections)
	return newService(client)
}

func startWorker(t *testing.T, s *Service) context.CancelFunc {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.StartWS(ctx, func(context.Context, domain.WSEvent) error { t.Error("device lifecycle callback called"); return nil })
	}()
	until(t, func() bool { s.mu.Lock(); defer s.mu.Unlock(); return s.running })
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("worker did not stop")
		}
	})
	return cancel
}

func TestWaitStartedSignalsWorkerWithoutChangingSubmitBeforeStart(t *testing.T) {
	s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {})
	if _, err := s.Submit(admitted(t, "before start")); !errors.Is(err, ErrNotStarted) {
		t.Fatalf("Submit before StartWS = %v, want ErrNotStarted", err)
	}

	startWorker(t, s)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := s.WaitStarted(ctx); err != nil {
		t.Fatalf("WaitStarted = %v, want nil", err)
	}
	if _, err := s.Submit(admitted(t, "after start")); err != nil {
		t.Fatalf("Submit after WaitStarted = %v, want nil", err)
	}
}

func TestWaitStartedHonorsCancellation(t *testing.T) {
	s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {})
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	if err := s.WaitStarted(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("WaitStarted without worker = %v, want context deadline", err)
	}
}

func until(t *testing.T, f func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !f() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(time.Millisecond)
	}
}

func admitted(t *testing.T, text string) Admission {
	t.Helper()
	a, err := AdmitTrustedRequest(internbridge.Request{Text: text, Operation: internbridge.Generate, DataClass: internbridge.Public})
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func terminal(t *testing.T, s *Service, id string) Turn {
	t.Helper()
	var turn Turn
	until(t, func() bool {
		var err error
		turn, err = s.Result(id)
		return err == nil && turn.State != "queued" && turn.State != "running"
	})
	return turn
}

func envelope(r internbridge.Request) map[string]any {
	hash := sha256.Sum256([]byte(r.RunID))
	return map[string]any{
		"version": internbridge.Version, "response_schema": internbridge.ResponseSchema,
		"run_id": "run-" + hex.EncodeToString(hash[:12]), "destination": internbridge.FirstContact,
		"requested_destination": "rex@dru", "kind": "persona", "status": "draft", "output": "Draft: " + r.Text,
		"executes_actions": false, "transport_status": "accepted", "lifecycle_status": "completed", "lifecycle_scope": "bridge_request",
		"reception_route": map[string]any{"first_destination": internbridge.FirstContact, "handoff": "rex@dru", "intent": "engineering", "status": "reception_route", "executed": false, "next_step": "safe_escalation"},
	}
}

func TestChatQueueCorrelation(t *testing.T) {
	var mu sync.Mutex
	var requests []internbridge.Request
	s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/intern" || r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("unexpected endpoint/credentials")
		}
		var req internbridge.Request
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
		}
		mu.Lock()
		requests = append(requests, req)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(envelope(req))
	})
	startWorker(t, s)
	if s.IsReady() || s.Version() != "" || s.AgentUptime() != 0 {
		t.Fatal("fabricated readiness")
	}
	s.SetBusy(true)
	ids := make([]string, QueueLimit)
	for i := range ids {
		id, err := s.Submit(admitted(t, "public sample"))
		if err != nil {
			t.Fatal(err)
		}
		ids[i] = id
	}
	if _, err := s.Submit(admitted(t, "overflow")); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	mu.Lock()
	count := len(requests)
	mu.Unlock()
	if count != 0 {
		t.Fatal("paused queue sent")
	}
	s.SetBusy(false)
	for _, id := range ids {
		got := terminal(t, s, id)
		if got.State != "completed" || got.RunID != id || got.Result == nil || got.Result.Output != "Draft: public sample" || got.ExecutesActions || got.Scope != "bridge_request" {
			t.Fatalf("bad result: %+v", got)
		}
		got.Result.Output = "mutated"
		again, _ := s.Result(id)
		if again.Result.Output == "mutated" {
			t.Fatal("result alias")
		}
	}
	if !s.IsReady() || s.ConnectedAt() == 0 || s.Version() != internbridge.Version {
		t.Fatal("generation not reflected in readiness")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(requests) != len(ids) {
		t.Fatal("missing/retried requests")
	}
	for i, r := range requests {
		if r.RunID != ids[i] || r.DataClass != internbridge.Public || r.Text != "public sample" {
			t.Fatal("queue changed custody/correlation")
		}
	}
}

func TestAdmissionAndUnsupportedNoEffects(t *testing.T) {
	var calls atomic.Int32
	s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(500) })
	startWorker(t, s)
	for _, c := range []internbridge.DataClass{internbridge.Unknown, internbridge.Restricted, internbridge.Secret, ""} {
		if _, err := AdmitTrustedRequest(internbridge.Request{Text: "held", Operation: internbridge.Generate, DataClass: c}); err == nil {
			t.Fatal("admitted held data")
		}
	}
	if _, err := s.Submit(Admission{}); err == nil {
		t.Fatal("zero admission accepted")
	}
	if _, err := s.SendChatMessage("unclassified"); !errors.Is(err, internbridge.ErrNeedsClassification) {
		t.Fatal(err)
	}
	// Exercise every explicitly unsupported error-returning interface method.
	v := reflect.ValueOf(s)
	for _, name := range []string{"QueuePendingEvent", "SetSessionKey", "SetupAgent", "AddChannel", "RefreshChannelConfig", "ResetAgent", "RestartAgent", "RefreshModelsConfig", "EnsureOnboarding", "SaveSkill", "InstallSkillArchive", "InstallSkillMarkdown", "ListSkills", "ReadSkillFiles", "ExportSkillArchive", "ReadSkillFile", "DeleteSkill", "FetchChatHistory", "GetConfigJSON", "WriteMCPEntry", "RemoveMCPEntry", "MarkGuardRun", "MarkBroadcastRun", "MarkPoseBucketRun", "Broadcast", "SendToUser", "SendToUserWithMedia", "SendToHALTTS", "Speak", "SendToHALTTSQueue", "StopTTS", "SetVolume", "StartHALVoice", "WatchIdentity", "UpdateIdentityName", "StartSkillWatcher", "StartModelSync", "UpdatePrimaryModel", "StartPrimaryModelWatch", "CompactSession", "NewSession", "GetTelegramTargets", "SendSlashCommandWithRun", "SendSlashCommandWithImagesAndRun"} {
		m := v.MethodByName(name)
		args := make([]reflect.Value, m.Type().NumIn())
		for i := range args {
			args[i] = reflect.Zero(m.Type().In(i))
		}
		out := m.Call(args)
		err, _ := out[len(out)-1].Interface().(error)
		if !errors.Is(err, domain.ErrNotSupportedByRuntime) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := s.SendChatMessageWithImages("photo", []string{"image"}); !errors.Is(err, domain.ErrNotSupportedByRuntime) {
		t.Fatal(err)
	}
	if len(s.SupportedChannels()) != 0 || s.GetTelegramBotToken() != "" || s.HasWhatsappSession("") || s.GetSessionKey() != "" {
		t.Fatal("fabricated capability")
	}
	events := s.PairWhatsapp(context.Background())
	e, ok := <-events
	if !ok || e.Status != domain.PairingStatusFailure {
		t.Fatal("pairing fake success")
	}
	if _, ok := <-events; ok {
		t.Fatal("pairing channel not closed")
	}
	if calls.Load() != 0 {
		t.Fatal("unsupported/custody operation reached bridge")
	}
}

func TestFailuresAreCorrelatedAndNotRetried(t *testing.T) {
	for _, kind := range []string{"protocol", "custody", "correlation", "fallback"} {
		t.Run(kind, func(t *testing.T) {
			var calls atomic.Int32
			s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var req internbridge.Request
				_ = json.NewDecoder(r.Body).Decode(&req)
				e := envelope(req)
				switch kind {
				case "protocol":
					e["executes_actions"] = true
				case "correlation":
					e["run_id"] = "run-000000000000000000000000"
				case "fallback":
					e["status"] = "fallback"
				case "custody":
					e["status"] = "custody_hold"
					e["requested_destination"] = internbridge.FirstContact
					delete(e, "output")
					e["reception_route"].(map[string]any)["handoff"] = nil
				}
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(e)
			})
			startWorker(t, s)
			id, err := s.Submit(admitted(t, "sample"))
			if err != nil {
				t.Fatal(err)
			}
			got := terminal(t, s, id)
			if got.State != "failed" || got.Result != nil || got.Error == "" || s.IsReady() || calls.Load() != 1 {
				t.Fatalf("bad failure: %+v calls=%d", got, calls.Load())
			}
		})
	}
}

func TestOfflineAndCancellation(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := uint16(listener.Addr().(*net.TCPAddr).Port)
	_ = listener.Close()
	client, _ := internbridge.New(port)
	s := newService(client)
	startWorker(t, s)
	id, err := s.Submit(admitted(t, "sample"))
	if err != nil {
		t.Fatal(err)
	}
	got := terminal(t, s, id)
	if got.Error != internbridge.ErrTransport.Error() || got.Result != nil || s.IsReady() {
		t.Fatalf("bad offline: %+v", got)
	}
	var calls atomic.Int32
	release := make(chan struct{})
	s2 := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) {
		// Drain the request so net/http can observe a disconnected client.
		_, _ = io.Copy(io.Discard, r.Body)
		calls.Add(1)
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	// Runs before fixture Close even if an assertion fails. Remote handlers
	// are not guaranteed to honor cancellation; cleanup must not assume it.
	t.Cleanup(func() { close(release) })
	cancel := startWorker(t, s2)
	id, _ = s2.Submit(admitted(t, "sample"))
	until(t, func() bool { return calls.Load() == 1 })
	cancel()
	got = terminal(t, s2, id)
	if got.State != "canceled" || !got.RemoteOutcomeUnknown {
		t.Fatalf("bad cancellation: %+v", got)
	}
}

func TestQueueDeadlineResultExpiryAndCapacity(t *testing.T) {
	var calls atomic.Int32
	s := bridgeFixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1) })
	cancel := startWorker(t, s)
	s.SetBusy(true)
	id, _ := s.Submit(admitted(t, "sample"))
	s.mu.Lock()
	s.queue[0].deadline = time.Now().Add(-time.Second)
	s.mu.Unlock()
	s.DrainPendingEvents()
	got := terminal(t, s, id)
	if got.Error != internbridge.ErrDeadline.Error() || got.RemoteOutcomeUnknown || calls.Load() != 0 {
		t.Fatalf("bad queue timeout: %+v", got)
	}
	s.mu.Lock()
	e := s.results[id]
	e.expires = time.Now().Add(-time.Second)
	s.results[id] = e
	s.mu.Unlock()
	if _, err := s.Result(id); !errors.Is(err, ErrResultNotFound) {
		t.Fatal(err)
	}
	s.mu.Lock()
	for i := 0; i < ResultLimit; i++ {
		_, key := s.NextChatRunID()
		s.results[key] = entry{turn: Turn{RunID: key}, expires: time.Now().Add(ResultTTL)}
	}
	s.mu.Unlock()
	if _, err := s.Submit(admitted(t, "sample")); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	cancel()
}
