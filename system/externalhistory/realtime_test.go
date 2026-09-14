package externalhistory

import "testing"

func TestRealtimeJournalRestartsAndDeduplicates(t *testing.T) {
	dir := t.TempDir()
	s, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	message := "[skills: input-branching]\n[HANDLED] Chào bạn\nHow are you?\n[REPLY] Tôi ổn.\n[snapshot:photo.jpg]"
	id, err := s.RecordRealtime("interaction-1", message)
	if err != nil {
		t.Fatal(err)
	}
	s, err = New(dir)
	if err != nil {
		t.Fatal(err)
	}
	r, ok := s.Lookup(id)
	if !ok || r.State != StatePending || r.Input != "Chào bạn\nHow are you?" || r.Output != "Tôi ổn.\n[snapshot:photo.jpg]" || r.Source != "realtime" {
		t.Fatalf("lost completed exchange after restart: %+v", r)
	}
	id2, err := s.RecordRealtime("interaction-1", message)
	if err != nil || id2 != id || len(s.Pending()) != 1 {
		t.Fatalf("duplicate: %s %v", id2, err)
	}
	if _, err = s.RecordRealtime("interaction-1", message+"changed"); err == nil {
		t.Fatal("accepted conflicting output")
	}
	g := &syncGateway{ready: true}
	s.Flush(g)
	if len(g.sent) != 1 || g.sent[0] != id {
		t.Fatal("pending exchange did not resume")
	}
	if err = s.Acknowledge(id); err != nil {
		t.Fatal(err)
	}
	if _, err = s.RecordRealtime("interaction-1", message); err != nil {
		t.Fatal(err)
	}
	s.Flush(g)
	if len(g.sent) != 1 {
		t.Fatal("replayed acknowledged exchange")
	}
}

func TestRealtimeJournalRejectsMalformedNotifications(t *testing.T) {
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, message := range []string{"hello", "[HANDLED] hello", "[REPLY] hello"} {
		if _, err := s.RecordRealtime("one", message); err == nil {
			t.Fatalf("accepted %q", message)
		}
	}
	if len(s.Records()) != 0 {
		t.Fatal("persisted malformed input")
	}
}

type steeringSyncGateway struct{ syncGateway }

func (*steeringSyncGateway) SupportsActiveTurnSteering() bool { return true }

func TestRealtimeJournalSteersOnlyCapableBusyRuntime(t *testing.T) {
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	pendingSync(t, s, "harness", "first")
	r := pendingSync(t, s, "realtime", "second")
	g := &steeringSyncGateway{syncGateway: syncGateway{ready: true, busy: true}}
	s.Flush(&g.syncGateway)
	if len(g.sent) != 0 {
		t.Fatal("steered unsupported runtime")
	}
	s.Flush(g)
	if len(g.sent) != 1 || g.sent[0] != r.SyncRunID {
		t.Fatal("did not select realtime for steering")
	}
	s.Flush(g)
	if len(g.sent) != 1 {
		t.Fatal("resent while awaiting acknowledgement")
	}
	if err = s.Acknowledge(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	s.Flush(g)
	if len(g.sent) != 1 {
		t.Fatal("steered Harness history")
	}
	g.busy = false
	s.Flush(g)
	if len(g.sent) != 2 {
		t.Fatal("lost pending Harness history")
	}
}
