package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Every runtime must instruct its agent to keep USER.md's `## Users` section
// current, and must teach the SAME format the reconciler prunes by.
//
// The instruction lives in each runtime's own onboarding block, and the blocks
// are not uniform — openclaw/codex/opencode/picoclaw carry it in HEARTBEAT.md,
// claudecode has no heartbeat loop so it carries a per-turn variant in CLAUDE.md,
// and hermes has neither HEARTBEAT.md nor KNOWLEDGE.md so it carries a variant in
// its SOUL.md block. That divergence is exactly why this is easy to miss: adding
// the instruction to "the runtimes" caught four of six on the first pass.
//
// A runtime without it silently stops maintaining per-person profiles the moment
// someone switches to it.
func TestEveryRuntimeTeachesThePeopleSync(t *testing.T) {
	root := filepath.Join("..", "..", "..", "runtimes")
	ents, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("read runtimes dir: %v", err)
	}

	var checked int
	for _, e := range ents {
		if !e.IsDir() {
			continue
		}
		src := filepath.Join(root, e.Name(), "onboarding.go")
		body, err := os.ReadFile(src)
		if err != nil {
			continue // not every dir is a runtime with an onboarding block
		}
		checked++
		text := string(body)

		// The format the reconciler keys on (see usersBlockRe): an enrollment
		// label plus a required role parenthetical.
		if !strings.Contains(text, "label> (friend)") {
			t.Errorf("%s: no people-sync instruction — this runtime would stop maintaining USER.md's ## Users section", e.Name())
			continue
		}
		// The constraints are the point. Without them the sync is the very
		// mechanism that fused two users into one profile.
		for _, want := range []string{
			"Only write what you observed about THAT person",
			"never carry a former user's traits",
			"never delete",
		} {
			if !strings.Contains(text, want) {
				t.Errorf("%s: people sync is missing constraint %q", e.Name(), want)
			}
		}
	}
	if checked < 6 {
		t.Fatalf("only inspected %d runtimes; expected at least 6", checked)
	}
}
