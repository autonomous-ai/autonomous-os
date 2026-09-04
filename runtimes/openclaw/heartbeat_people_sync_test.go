package openclaw

import (
	"regexp"
	"strings"
	"testing"
)

// The daily people sync writes into USER.md, which the OS then reconciles
// against the enrollment store. If the format the agent is told to write drifts
// from the format migratepersona.usersBlockRe parses, entries become
// unprunable — a retired person would live in the prompt forever. This pins the
// contract from the instruction side.
func TestHeartbeatPeopleSyncFormatMatchesTheReconciler(t *testing.T) {
	// Same expression as migratepersona.usersBlockRe (kept in sync deliberately:
	// the two packages must not import each other for a prompt string).
	usersBlock := regexp.MustCompile(`(?i)^(?:Users:\s*)?\*\*([^*(]+?)\s*\([^)]*\)\*\*`)

	if !strings.Contains(heartbeatMDBlock, "- **<label> (friend)** — call: …; notes: …") {
		t.Fatal("the instruction no longer specifies the person-entry format")
	}
	// What the agent is told to write, with the placeholder filled in. The
	// segment form must parse the same as the older prose form did — the
	// reconciler only ever keys on the leading **label (role)**.
	for _, entry := range []string{
		"**long (friend)** — call: anh Long; notes: hunches at the screen",
		"Users: **long (friend)** — call: anh Long; notes: hunches", // after heading flattening
		"**long (friend)**: prefers Vietnamese",                     // legacy prose entries still prune
	} {
		m := usersBlock.FindStringSubmatch(entry)
		if m == nil {
			t.Errorf("reconciler cannot parse an entry in the taught format: %q", entry)
			continue
		}
		if m[1] != "long" {
			t.Errorf("label parsed as %q, want %q", m[1], "long")
		}
	}
}

// The constraints are the point of this block: without them the sync is exactly
// the mechanism that fused two users into one profile in the first place.
func TestHeartbeatPeopleSyncCarriesItsConstraints(t *testing.T) {
	for _, want := range []string{
		"Only write what you observed about THAT person", // no cross-attribution
		"never carry a former user's traits",             // the lamp-ac82 failure
		"Never delete a PERSON's entry",                  // absence is not departure
		"Do NOT fill",                                    // Name stays the enrollment tag's job
	} {
		if !strings.Contains(heartbeatMDBlock, want) {
			t.Errorf("people sync lost its constraint: %q", want)
		}
	}
}

// The synthesis must NOT be gated on a wall-clock hour. A desk device is often
// switched off before evening, so "at 21:00" never arrives and whole days go
// undistilled (device-observed: memory/2026-08-24.md was never synthesized).
// The gate is "is there a finished day that was never distilled", which any
// heartbeat can satisfy.
func TestHeartbeatSynthesisIsCatchUpNotClockGated(t *testing.T) {
	if strings.Contains(heartbeatMDBlock, "If current time is >= 21:00 AND") {
		t.Error("synthesis is gated on a fixed hour again — it will not fire on a device switched off in the evening")
	}
	for _, want := range []string{
		"catch-up",               // the gate is a backlog check
		"every day BEFORE today", // never distil a day still in progress
	} {
		if !strings.Contains(heartbeatMDBlock, want) {
			t.Errorf("catch-up gate lost its wording: %q", want)
		}
	}
}
