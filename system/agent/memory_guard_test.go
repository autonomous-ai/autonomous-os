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
	g, path := seedGuard(t, "- **Name:**\n"+poison)
	g.onChange(path, "watch") // first: quarantines and records the hash of what it wrote
	if n := countBackups(t, path); n != 1 {
		t.Fatalf("want one backup after the first change, got %d", n)
	}
	g.onChange(path, "watch") // the rename of our own temp file lands as an event
	if n := countBackups(t, path); n != 1 {
		t.Fatalf("own write must not trigger a second pass, got %d backups", n)
	}
}

func countBackups(t *testing.T, path string) int {
	t.Helper()
	m, _ := filepath.Glob(path + ".bak-*")
	return len(m)
}
