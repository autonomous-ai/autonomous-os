package migratepersona

import (
	"path/filepath"
	"strings"
	"testing"
)

// Every registered runtime must contribute paths. The interface forces the
// method to exist; this catches the other half — a new adapter that satisfies
// the compiler with `return nil` and so keeps its persona through a factory
// reset.
func TestEveryAdapterContributesPersonaPaths(t *testing.T) {
	opts := DefaultOptions("", "")
	for rt, a := range adapters {
		paths := a.personaPaths(opts)
		if len(paths) == 0 {
			t.Errorf("runtime %q returns no persona paths — its profile would survive a factory reset", rt)
			continue
		}
		for _, p := range paths {
			if !filepath.IsAbs(p) {
				t.Errorf("runtime %q persona path %q is not absolute", rt, p)
			}
		}
	}
}

// The reason this package exists: a retired user's USER.md must be reachable in
// every runtime tree, not just the active one.
func TestPersonaPathsCoverEveryRuntimeUserProfile(t *testing.T) {
	got := PersonaPaths(DefaultOptions("", ""))
	index := make(map[string]bool, len(got))
	for _, p := range got {
		index[p] = true
	}

	for _, want := range []string{
		"/root/.openclaw/workspace/USER.md",
		"/root/.picoclaw/workspace/USER.md",
		"/root/.claudecode/workspace/USER.md",
		"/root/.opencode/workspace/USER.md",
		"/root/.hermes/memories",
	} {
		if !index[want] {
			t.Errorf("PersonaPaths missing %q — a stale profile there survives factory reset", want)
		}
	}
}

// A persona wipe must not take the runtime installation with it. Hermes keeps
// its install and logs directly under the home root, and every other runtime
// keeps skills/ + configs/ in the workspace, so neither dir may be listed.
func TestPersonaPathsNeverWipeARuntimeRoot(t *testing.T) {
	for _, p := range PersonaPaths(DefaultOptions("", "")) {
		switch p {
		case "/root/.hermes",
			"/root/.openclaw", "/root/.openclaw/workspace",
			"/root/.picoclaw", "/root/.picoclaw/workspace",
			"/root/.codex", "/root/.codex/workspace",
			"/root/.claudecode", "/root/.claudecode/workspace",
			"/root/.opencode", "/root/.opencode/workspace":
			t.Errorf("PersonaPaths lists runtime root/workspace %q — that removes the installation, skills and configs, not just the persona", p)
		}
	}
}

func TestPersonaPathsAreDedupedAndSorted(t *testing.T) {
	got := PersonaPaths(DefaultOptions("", ""))
	for i := 1; i < len(got); i++ {
		if got[i-1] == got[i] {
			t.Fatalf("duplicate path %q", got[i])
		}
		if got[i-1] > got[i] {
			t.Fatalf("not sorted: %q before %q", got[i-1], got[i])
		}
	}
}

// A caller that supplies its own dirs (tests, a device with a relocated
// OpenClaw config) must get those, not the hardcoded defaults.
func TestPersonaPathsHonourCustomRoots(t *testing.T) {
	opts := DefaultOptions("/tmp/oc", "/tmp/hermes")
	got := strings.Join(PersonaPaths(opts), "\n")
	if !strings.Contains(got, "/tmp/oc/workspace/USER.md") {
		t.Errorf("custom openclaw dir ignored:\n%s", got)
	}
	if !strings.Contains(got, "/tmp/hermes/memories") {
		t.Errorf("custom hermes root ignored:\n%s", got)
	}
}
