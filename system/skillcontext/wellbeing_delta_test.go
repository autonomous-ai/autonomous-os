package skillcontext

import (
	"testing"
	"time"

	"go.autonomous.ai/os/system/skillcontext/wellbeing"
)

func TestMinutesSinceFirstActivity(t *testing.T) {
	now := time.Now()
	ev := func(action string, agoMin int) wellbeing.Event {
		return wellbeing.Event{TS: float64(now.Add(-time.Duration(agoMin) * time.Minute).Unix()), Action: action}
	}
	if got := minutesSinceFirstActivity(nil, now); got != -1 {
		t.Fatalf("empty: got %d, want -1", got)
	}
	// Presence/nudge rows are skipped; the first sedentary row is the anchor.
	events := []wellbeing.Event{ev("leave", 90), ev("using computer", 40), ev("writing", 10)}
	if got := minutesSinceFirstActivity(events, now); got != 40 {
		t.Fatalf("got %d, want 40", got)
	}
	if got := minutesSinceFirstActivity([]wellbeing.Event{ev("nudge_break", 50)}, now); got != -1 {
		t.Fatalf("nudge-only: got %d, want -1", got)
	}
}
