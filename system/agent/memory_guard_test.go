package agent

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/lib/usercanon"
)

func seedGuard(t *testing.T, userMD string) (*MemoryGuard, string) {
	t.Helper()
	root := t.TempDir()
	users := filepath.Join(root, "users")
	if err := os.MkdirAll(filepath.Join(users, "long"), 0o755); err != nil {
		t.Fatal(err)
	}
	prev := usercanon.UsersDir
	usercanon.UsersDir = users
	t.Cleanup(func() { usercanon.UsersDir = prev })

	ws := filepath.Join(root, "openclaw", "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(ws, "USER.md")
	if err := os.WriteFile(path, []byte(userMD), 0o644); err != nil {
		t.Fatal(err)
	}
	g := &MemoryGuard{
		opts:        migratepersona.Options{OpenclawWorkspace: ws},
		execute:     true,
		runtime:     migratepersona.RuntimeOpenclaw,
		lastWritten: map[string]string{},
	}
	return g, path
}

const poison = "- Talks about an Obsidian vault, wants hands-on action done\n"

func TestRunQuarantinesAndPublishesState(t *testing.T) {
	g, path := seedGuard(t, "- **Name:**\n"+poison)
	g.Run("test")
	got, _ := os.ReadFile(path)
	if strings.Contains(string(got), "Obsidian") {
		t.Fatalf("poison survived Run:\n%s", got)
	}
	st := migratepersona.MemoryState()
	if st == nil || st["USER.md"] == nil {
		t.Fatalf("Run must publish the active runtime's state, got %v", st)
	}
}

func TestOnChangeSkipsTheGuardsOwnWrite(t *testing.T) {
	content := "- **Name:**\n" + poison
	g, path := seedGuard(t, content)

	// Pretend the on-disk content is what the guard itself just wrote: the
	// hash short-circuit must run BEFORE GuardMemoryFile, so the poison stays
	// and nothing is backed up even though the file would otherwise be swept.
	g.lastWritten[path] = migratepersona.Sha8([]byte(content))
	g.onChange(path, "watch")
	if got, _ := os.ReadFile(path); !strings.Contains(string(got), "Obsidian") {
		t.Fatalf("own write must be left alone, but the guard rewrote it:\n%s", got)
	}
	if n := countBackups(t, path); n != 0 {
		t.Fatalf("own write must not be guarded, got %d backups", n)
	}

	// Forget the hash: now the same content is an agent write and is swept.
	delete(g.lastWritten, path)
	g.onChange(path, "watch")
	if got, _ := os.ReadFile(path); strings.Contains(string(got), "Obsidian") {
		t.Fatalf("poison survived onChange:\n%s", got)
	}
	if n := countBackups(t, path); n != 1 {
		t.Fatalf("want one backup after the agent write, got %d", n)
	}

	// The rename of our own rewrite lands as a watch event: no second pass.
	g.onChange(path, "watch")
	if n := countBackups(t, path); n != 1 {
		t.Fatalf("own rewrite must not trigger a second pass, got %d backups", n)
	}
}

func countBackups(t *testing.T, path string) int {
	t.Helper()
	m, _ := filepath.Glob(path + ".bak-*")
	return len(m)
}
