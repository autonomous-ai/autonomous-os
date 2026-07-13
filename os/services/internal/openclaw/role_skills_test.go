package openclaw

import (
	"archive/zip"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/internal/skills"
)

func TestInstallRoleSkillsInvalidRole(t *testing.T) {
	// Path-escaping / malformed slugs must be rejected before any network call.
	for _, role := range []string{"", "../etc", "Bad", "a/b", "x.y", "UP", "a b"} {
		if _, err := InstallRoleSkills(t.TempDir(), role); !errors.Is(err, ErrInvalidRole) {
			t.Errorf("role %q: want ErrInvalidRole, got %v", role, err)
		}
	}
}

func TestEnsureMCPSkillResolvesAgainstSharedStore(t *testing.T) {
	// A connector skill seeded in the shared store must satisfy the
	// already-installed check no matter which configDir the caller passes: an MCP
	// skill set up while another runtime was active used to land in OpenClaw's
	// workspace, where the live runtime never looked for it. A hit here means no
	// network call — the download path would fail this test offline.
	store := skills.SetDirForTest(t, filepath.Join(t.TempDir(), "store"))
	if err := os.MkdirAll(filepath.Join(store, "figma-api"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(store, "figma-api", "SKILL.md"), []byte("# figma"), 0644); err != nil {
		t.Fatal(err)
	}

	if err := EnsureMCPSkill(t.TempDir(), "figma-api"); err != nil {
		t.Fatalf("EnsureMCPSkill: %v", err)
	}
}

func TestExtractDirFromZip(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skills.zip")

	f, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	w := zip.NewWriter(f)
	write := func(name, body string) {
		wr, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := wr.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	write("skills/foo/SKILL.md", "hello")
	write("skills/bar.txt", "x")
	write("other/ignore.txt", "should be skipped")
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()

	dest := filepath.Join(dir, "out")
	n, err := extractDirFromZip(zipPath, "skills/", dest)
	if err != nil {
		t.Fatalf("extractDirFromZip: %v", err)
	}
	if n != 2 {
		t.Errorf("file count = %d, want 2", n)
	}
	// Prefix stripped: skills/foo/SKILL.md -> <dest>/foo/SKILL.md
	if b, err := os.ReadFile(filepath.Join(dest, "foo", "SKILL.md")); err != nil || string(b) != "hello" {
		t.Errorf("foo/SKILL.md: body=%q err=%v", b, err)
	}
	if _, err := os.Stat(filepath.Join(dest, "bar.txt")); err != nil {
		t.Errorf("bar.txt missing: %v", err)
	}
	// Entries outside the prefix are not extracted.
	if _, err := os.Stat(filepath.Join(dest, "ignore.txt")); !os.IsNotExist(err) {
		t.Errorf("ignore.txt should not be extracted")
	}
}
