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

	if !strings.Contains(heartbeatMDBlock, "- **<label> (friend)**:") {
		t.Fatal("the instruction no longer specifies the person-entry format")
	}
	// What the agent is told to write, with the placeholder filled in.
	for _, entry := range []string{
		"**long (friend)**: prefers Vietnamese",
		"Users: **long (friend)**: prefers Vietnamese", // after heading flattening
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
		"Update and add only — never delete",             // absence is not departure
		"Do NOT fill",                                    // Name stays the enrollment tag's job
	} {
		if !strings.Contains(heartbeatMDBlock, want) {
			t.Errorf("people sync lost its constraint: %q", want)
		}
	}
}
