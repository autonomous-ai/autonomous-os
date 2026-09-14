package externalhistory

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func beginTest(t *testing.T, s *Store, id string) Record {
	t.Helper()
	r, err := s.Begin(Record{Source: "harness", OriginRunID: id, AgentID: "agent-1", AgentName: "Agent One", Input: "hello"})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestLifecycleAndRestart(t *testing.T) {
	s := newTestStore(t)
	r := beginTest(t, s, "one")
	if err := s.Acknowledge(r.SyncRunID); err == nil {
		t.Fatal("acknowledged waiting record")
	}
	if err := s.MarkSending(r.SyncRunID); err == nil {
		t.Fatal("sent waiting record")
	}
	if err := s.Complete("harness", "one", "world"); err != nil {
		t.Fatal(err)
	}
	if err := s.Acknowledge(r.SyncRunID); err == nil {
		t.Fatal("acknowledged pending record")
	}
	restored, err := New(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := restored.Pending(); len(got) != 1 || got[0].Output != "world" {
		t.Fatalf("pending after restart: %+v", got)
	}
	if err := restored.MarkSending(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	restarted, err := New(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := restarted.Lookup(r.SyncRunID)
	if got.State != StateUncertain || len(restarted.Pending()) != 0 {
		t.Fatal("sending must become uncertain, never retried")
	}
	if err := restarted.MarkSending(r.SyncRunID); err == nil {
		t.Fatal("resent uncertain record")
	}
	if err := restarted.Acknowledge(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	if err := restarted.Acknowledge(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	final, err := New(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	got, _ = final.Lookup(r.SyncRunID)
	if got.State != StateDone {
		t.Fatalf("got %s", got.State)
	}
}

func TestIdempotencyAndConflicts(t *testing.T) {
	s := newTestStore(t)
	r := beginTest(t, s, "one")
	duplicate := beginTest(t, s, "one")
	if duplicate != r {
		t.Fatal("begin is not idempotent")
	}
	for _, change := range []func(*Record){func(r *Record) { r.Input = "different" }, func(r *Record) { r.AgentID = "other" }, func(r *Record) { r.AgentName = "other" }} {
		conflict := r
		change(&conflict)
		if _, err := s.Begin(conflict); err == nil {
			t.Fatal("accepted conflicting turn")
		}
	}
	if err := s.Complete(r.Source, r.OriginRunID, "answer"); err != nil {
		t.Fatal(err)
	}
	if err := s.Complete(r.Source, r.OriginRunID, "answer"); err != nil {
		t.Fatal(err)
	}
	if err := s.Complete(r.Source, r.OriginRunID, "other"); err == nil {
		t.Fatal("accepted conflicting output")
	}
	if err := s.Complete(r.Source, "missing", "answer"); err == nil {
		t.Fatal("completed unknown record")
	}
	r.Source = "other"
	if other, err := s.Begin(r); err != nil || other.SyncRunID == duplicate.SyncRunID {
		t.Fatalf("source isolation: %+v %v", other, err)
	}
}

func TestBoundsAndPruning(t *testing.T) {
	s := newTestStore(t)
	if _, err := s.Begin(Record{Source: "test", OriginRunID: "large", Input: strings.Repeat("x", MaxInputBytes+1)}); err == nil {
		t.Fatal("accepted oversized input")
	}
	r := beginTest(t, s, "one")
	if err := s.Complete(r.Source, r.OriginRunID, strings.Repeat("x", MaxOutputBytes+1)); err == nil {
		t.Fatal("accepted oversized output")
	}
	// Fill memory without spending a thousand disk syncs; Begin enforces the same capacity.
	for i := 1; i < MaxRecords; i++ {
		id := fmt.Sprintf("other-%d", i)
		s.records[id] = Record{SyncRunID: id, State: StateWaiting}
	}
	if _, err := s.Begin(Record{Source: "test", OriginRunID: "overflow"}); err == nil {
		t.Fatal("accepted record past capacity")
	}
	old := r
	old.State = StateDone
	old.UpdatedAt = time.Now().Add(-retention - time.Hour)
	if err := s.persist(old); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Begin(Record{Source: "test", OriginRunID: "after-prune"}); err != nil {
		t.Fatal(err)
	}
	if _, ok := s.Lookup(r.SyncRunID); ok {
		t.Fatal("expired tombstone not pruned")
	}
	if _, err := os.Stat(filepath.Join(s.dir, r.SyncRunID+".json")); !os.IsNotExist(err) {
		t.Fatalf("expired file remains: %v", err)
	}
}

func TestPersistenceFailureDoesNotAdvanceMemory(t *testing.T) {
	s := newTestStore(t)
	r := beginTest(t, s, "one")
	original := s.dir
	s.dir = filepath.Join(s.dir, "missing")
	if err := s.Complete(r.Source, r.OriginRunID, "answer"); err == nil {
		t.Fatal("write unexpectedly succeeded")
	}
	got, _ := s.Lookup(r.SyncRunID)
	if got != r {
		t.Fatal("failed persistence changed memory")
	}
	s.dir = original
	restored, err := New(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	got, _ = restored.Lookup(r.SyncRunID)
	if got != r {
		t.Fatal("failed persistence changed disk")
	}
}

func TestPendingOrderAndPrivateFiles(t *testing.T) {
	s := newTestStore(t)
	first := beginTest(t, s, "first")
	second := beginTest(t, s, "second")
	beginTest(t, s, "waiting")
	for _, r := range []Record{second, first} {
		if err := s.Complete(r.Source, r.OriginRunID, "ok"); err != nil {
			t.Fatal(err)
		}
	}
	pending := s.Pending()
	if len(pending) != 2 || pending[0].SyncRunID != first.SyncRunID || pending[1].SyncRunID != second.SyncRunID {
		t.Fatalf("wrong order: %+v", pending)
	}
	info, err := os.Stat(filepath.Join(s.dir, first.SyncRunID+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("file mode %o", info.Mode().Perm())
	}
}

func TestMarkUncertainPreservesTerminalAck(t *testing.T) {
	s := newTestStore(t)
	r := beginTest(t, s, "one")
	if err := s.MarkUncertain(r.SyncRunID); err == nil {
		t.Fatal("waiting became uncertain")
	}
	if err := s.Complete(r.Source, r.OriginRunID, "answer"); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkUncertain(r.SyncRunID); err == nil {
		t.Fatal("pending became uncertain")
	}
	if err := s.MarkSending(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkUncertain(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	restarted, err := New(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := restarted.Lookup(r.SyncRunID)
	if got.State != StateUncertain || len(restarted.Pending()) != 0 {
		t.Fatal("uncertain state was lost")
	}
	if err := restarted.Acknowledge(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	if err := restarted.MarkUncertain(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	got, _ = restarted.Lookup(r.SyncRunID)
	if got.State != StateDone {
		t.Fatal("late transport failure reverted terminal ack")
	}
}

func TestStartupRejectsCorruptRecord(t *testing.T) {
	s := newTestStore(t)
	r := beginTest(t, s, "one")
	if err := os.WriteFile(filepath.Join(s.dir, r.SyncRunID+".json"), []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := New(s.dir); err == nil {
		t.Fatal("silently accepted corrupt journal")
	}
}

func TestCapacityEvictsOldestCompletedOnly(t *testing.T) {
	s := newTestStore(t)
	oldest := beginTest(t, s, "oldest")
	recent := beginTest(t, s, "recent")
	oldest.State = StateDone
	oldest.UpdatedAt = time.Now().Add(-time.Hour)
	recent.State = StateDone
	if err := s.persist(oldest); err != nil {
		t.Fatal(err)
	}
	if err := s.persist(recent); err != nil {
		t.Fatal(err)
	}
	// Keep unsynced records in memory to test capacity without 1024 fsyncs.
	for i := 2; i < MaxRecords; i++ {
		id := fmt.Sprintf("unfinished-%d", i)
		states := []string{StateWaiting, StatePending, StateSending, StateUncertain}
		s.records[id] = Record{SyncRunID: id, State: states[i%len(states)], UpdatedAt: time.Now().Add(-2 * retention)}
	}
	duplicate, err := s.Begin(recent)
	if err != nil || duplicate != recent {
		t.Fatalf("retained duplicate: %+v %v", duplicate, err)
	}
	beginTest(t, s, "new")
	if _, ok := s.Lookup(oldest.SyncRunID); ok {
		t.Fatal("oldest done record not evicted")
	}
	if _, ok := s.Lookup(recent.SyncRunID); !ok {
		t.Fatal("recent done record evicted")
	}
	if len(s.Records()) != MaxRecords {
		t.Fatal("unexpected record count")
	}
	if _, err := os.Stat(filepath.Join(s.dir, oldest.SyncRunID+".json")); !os.IsNotExist(err) {
		t.Fatalf("evicted file remains: %v", err)
	}
	for i := 2; i < MaxRecords; i++ {
		if _, ok := s.Lookup(fmt.Sprintf("unfinished-%d", i)); !ok {
			t.Fatal("evicted unfinished record")
		}
	}
}
