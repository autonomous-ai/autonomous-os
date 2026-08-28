package mqtthandler

import (
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/schedule"
)

func TestHandleScheduleRun_UnknownIdAcksFailure(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))

	_, errMsg := resolveScheduleRun(store, "does-not-exist")
	if errMsg == "" {
		t.Fatal("expected a failure message for an unknown schedule id")
	}

	// The store must still be perfectly usable after a miss — a lookup failure
	// must never corrupt or wedge it.
	if _, err := store.Load(); err != nil {
		t.Fatalf("store unusable after a miss: %v", err)
	}
}

func TestResolveScheduleRunRequiresID(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	if _, errMsg := resolveScheduleRun(store, "   "); errMsg == "" {
		t.Fatal("a blank id must be rejected")
	}
}

func TestResolveScheduleRunFindsExisting(t *testing.T) {
	store := schedule.NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	if err := store.Replace([]schedule.Schedule{{ID: "s1", Name: "x"}}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	sch, errMsg := resolveScheduleRun(store, "s1")
	if errMsg != "" {
		t.Fatalf("unexpected error: %s", errMsg)
	}
	if sch.ID != "s1" {
		t.Fatalf("got %+v", sch)
	}
}
