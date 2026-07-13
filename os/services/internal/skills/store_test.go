package skills

import (
	"os"
	"path/filepath"
	"testing"
)

// withTempStore points the store at a temp dir for the duration of a test, so no
// test ever touches the real /root store.
func withTempStore(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "store")
	old := storeDir
	storeDir = dir
	t.Cleanup(func() { storeDir = old })
	return dir
}

func writeSkill(t *testing.T, root, name, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(root, name), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, name, "SKILL.md"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLinkRuntimeDir_MigratesExistingDirIntoStore(t *testing.T) {
	store := withTempStore(t)
	runtimeDir := filepath.Join(t.TempDir(), "workspace", "skills")
	writeSkill(t, runtimeDir, "connectors", "x")

	changed, err := LinkRuntimeDir(runtimeDir)
	if err != nil {
		t.Fatalf("LinkRuntimeDir: %v", err)
	}
	if !changed {
		t.Fatal("want changed=true on first link")
	}

	// The skill moved into the store...
	if _, err := os.Stat(filepath.Join(store, "connectors", "SKILL.md")); err != nil {
		t.Fatalf("skill not migrated into store: %v", err)
	}
	// ...and the runtime path now resolves to the store.
	target, err := os.Readlink(runtimeDir)
	if err != nil {
		t.Fatalf("runtime dir is not a symlink: %v", err)
	}
	if target != store {
		t.Fatalf("symlink target = %q, want %q", target, store)
	}
}

func TestLinkRuntimeDir_IsIdempotent(t *testing.T) {
	withTempStore(t)
	runtimeDir := filepath.Join(t.TempDir(), "skills")

	if _, err := LinkRuntimeDir(runtimeDir); err != nil {
		t.Fatal(err)
	}
	changed, err := LinkRuntimeDir(runtimeDir)
	if err != nil {
		t.Fatal(err)
	}
	if changed {
		t.Fatal("want changed=false when the link is already correct")
	}
}

// The store copy is the one the skill watcher keeps fresh; a copy still sitting in
// a runtime's own dir is the stale duplicate. The store must win.
func TestLinkRuntimeDir_DoesNotClobberSkillAlreadyInStore(t *testing.T) {
	store := withTempStore(t)
	writeSkill(t, store, "connectors", "fresh")

	runtimeDir := filepath.Join(t.TempDir(), "skills")
	writeSkill(t, runtimeDir, "connectors", "stale")

	if _, err := LinkRuntimeDir(runtimeDir); err != nil {
		t.Fatal(err)
	}

	got, err := os.ReadFile(filepath.Join(store, "connectors", "SKILL.md"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "fresh" {
		t.Fatalf("store copy = %q, want the store to win over the runtime duplicate", got)
	}
}

// A dir that is already a symlink but points somewhere else must be re-pointed.
func TestLinkRuntimeDir_RepointsWrongSymlink(t *testing.T) {
	store := withTempStore(t)
	tmp := t.TempDir()
	elsewhere := filepath.Join(tmp, "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o755); err != nil {
		t.Fatal(err)
	}
	runtimeDir := filepath.Join(tmp, "skills")
	if err := os.Symlink(elsewhere, runtimeDir); err != nil {
		t.Fatal(err)
	}

	changed, err := LinkRuntimeDir(runtimeDir)
	if err != nil {
		t.Fatalf("LinkRuntimeDir: %v", err)
	}
	if !changed {
		t.Fatal("want changed=true when re-pointing a wrong symlink")
	}
	target, err := os.Readlink(runtimeDir)
	if err != nil {
		t.Fatal(err)
	}
	if target != store {
		t.Fatalf("symlink target = %q, want %q", target, store)
	}
}

func TestPruneUnsupported_RemovesOnlyUnlisted(t *testing.T) {
	store := withTempStore(t)
	writeSkill(t, store, "connectors", "x")
	writeSkill(t, store, "led-control", "x")

	removed := PruneUnsupported(map[string]bool{"connectors": true})

	if len(removed) != 1 || removed[0] != "led-control" {
		t.Fatalf("removed = %v, want [led-control]", removed)
	}
	if _, err := os.Stat(filepath.Join(store, "connectors")); err != nil {
		t.Fatalf("supported skill was pruned: %v", err)
	}
	if _, err := os.Stat(filepath.Join(store, "led-control")); !os.IsNotExist(err) {
		t.Fatal("unsupported skill survived the prune")
	}
}

// The store is SHARED, so a prune must never delete a skill it doesn't own.
// Runtime-bundled skills (picoclaw's tmux, agent-browser, …) and MCP connector
// skills (figma-api) are not platform-catalog entries; pruning by "not in keep"
// alone would make whichever runtime booted last wipe the others' skills.
func TestPruneUnsupported_LeavesNonCatalogSkillsAlone(t *testing.T) {
	store := withTempStore(t)
	writeSkill(t, store, "led-control", "x") // catalog, unsupported -> pruned
	writeSkill(t, store, "figma-api", "x")   // MCP connector skill -> must survive
	writeSkill(t, store, "tmux", "x")        // picoclaw bundled skill -> must survive

	removed := PruneUnsupported(map[string]bool{"connectors": true})

	if len(removed) != 1 || removed[0] != "led-control" {
		t.Fatalf("removed = %v, want only [led-control]", removed)
	}
	for _, name := range []string{"figma-api", "tmux"} {
		if _, err := os.Stat(filepath.Join(store, name)); err != nil {
			t.Fatalf("non-catalog skill %q was pruned from the shared store: %v", name, err)
		}
	}
}

func TestInstallByName_NoBaseURLIsANoOp(t *testing.T) {
	withTempStore(t)
	if got := InstallByName("", []string{"connectors"}); got != nil {
		t.Fatalf("InstallByName with no base URL = %v, want nil", got)
	}
}
