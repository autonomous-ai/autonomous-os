package skills

import (
	"archive/zip"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRenderSkillMarkdown(t *testing.T) {
	got := RenderSkillMarkdown("weekly-status-report",
		"Summarise the week's activity.", "1. Collect\n2. Group\n")

	want := "---\nname: weekly-status-report\ndescription: Summarise the week's activity.\n---\n\n1. Collect\n2. Group\n"
	if got != want {
		t.Fatalf("rendered SKILL.md mismatch:\ngot:\n%q\nwant:\n%q", got, want)
	}
}

// A newline in the description would terminate the unquoted YAML scalar and
// corrupt the front-matter block, so it must be flattened.
func TestRenderSkillMarkdownFlattensDescription(t *testing.T) {
	got := RenderSkillMarkdown("x", "line one\nline two\t  spaced", "body")

	if !strings.Contains(got, "description: line one line two spaced\n") {
		t.Fatalf("description not flattened to one line:\n%s", got)
	}
	// Exactly the three front-matter delimiters, nothing extra.
	if n := strings.Count(got, "---\n"); n != 2 {
		t.Errorf("front-matter delimiters = %d, want 2:\n%s", n, got)
	}
}

func TestWriteAuthoredSkill(t *testing.T) {
	dir := t.TempDir()

	path, err := WriteAuthoredSkill(dir, "my-skill", "Does a thing.", "Do the thing.")
	if err != nil {
		t.Fatalf("write: %v", err)
	}
	if want := filepath.Join(dir, "my-skill", "SKILL.md"); path != want {
		t.Errorf("path = %q, want %q", path, want)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if !strings.HasPrefix(string(content), "---\nname: my-skill\n") {
		t.Errorf("unexpected content:\n%s", content)
	}
}

func TestWriteAuthoredSkillRejectsBadInput(t *testing.T) {
	cases := []struct {
		label                           string
		name, description, instructions string
		wantErr                         error
	}{
		{"empty name", "", "d", "i", ErrInvalidSkillName},
		{"uppercase", "MySkill", "d", "i", ErrInvalidSkillName},
		{"traversal", "../evil", "d", "i", ErrInvalidSkillName},
		{"slash", "a/b", "d", "i", ErrInvalidSkillName},
		{"too long", strings.Repeat("a", 65), "d", "i", ErrInvalidSkillName},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			dir := t.TempDir()
			if _, err := WriteAuthoredSkill(dir, tc.name, tc.description, tc.instructions); !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			entries, _ := os.ReadDir(dir)
			if len(entries) != 0 {
				t.Errorf("nothing should have been written, found %d entries", len(entries))
			}
		})
	}

	dir := t.TempDir()
	if _, err := WriteAuthoredSkill(dir, "ok", "  ", "i"); err == nil {
		t.Error("blank description must be rejected")
	}
	if _, err := WriteAuthoredSkill(dir, "ok", "d", "\n\t "); err == nil {
		t.Error("blank instructions must be rejected")
	}
}

// Authoring must never clobber a store- or OTA-installed skill of the same name.
func TestWriteAuthoredSkillRefusesToOverwrite(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "music", "SKILL.md")
	if err := os.MkdirAll(filepath.Dir(existing), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(existing, []byte("ORIGINAL"), 0644); err != nil {
		t.Fatal(err)
	}

	if _, err := WriteAuthoredSkill(dir, "music", "d", "i"); !errors.Is(err, ErrSkillExists) {
		t.Fatalf("err = %v, want ErrSkillExists", err)
	}
	content, _ := os.ReadFile(existing)
	if string(content) != "ORIGINAL" {
		t.Errorf("existing skill was modified: %q", content)
	}
}

// ─── Install ─────────────────────────────────────────────────────────────────

func makeZip(t *testing.T, path string, entries map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create zip: %v", err)
	}
	defer f.Close()
	w := zip.NewWriter(f)
	for name, content := range entries {
		e, err := w.Create(name)
		if err != nil {
			t.Fatalf("entry %s: %v", name, err)
		}
		if _, err := e.Write([]byte(content)); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close zip: %v", err)
	}
}

// The catalog's `.skill` archives wrap everything in a single <name>/ dir; that
// segment names the skill and must be stripped, not nested twice.
func TestInstallSkillArchiveStripsWrappingDir(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{
		"design-critique/SKILL.md":            "# Design Critique\n",
		"design-critique/reference/notes.md":  "notes",
		"design-critique/.DS_Store":           "junk",
		"__MACOSX/design-critique/._SKILL.md": "junk",
	})
	skillsDir := filepath.Join(tmp, "skills")

	dir, count, err := InstallSkillArchive(archive, skillsDir, "fallback")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if want := filepath.Join(skillsDir, "design-critique"); dir != want {
		t.Fatalf("dir = %q, want %q", dir, want)
	}
	if count != 2 {
		t.Errorf("count = %d, want 2 (cruft filtered)", count)
	}
	if _, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		t.Errorf("SKILL.md not at the skill root — wrapping dir was not stripped: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "reference", "notes.md")); err != nil {
		t.Errorf("nested file missing: %v", err)
	}
	// No staging leftovers.
	if _, err := os.Stat(dir + ".new"); !os.IsNotExist(err) {
		t.Error("staging dir was left behind")
	}
}

// OTA-style archives put files at the root, so fallbackName names the skill.
func TestInstallSkillArchiveUsesFallbackName(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{"SKILL.md": "body"})
	skillsDir := filepath.Join(tmp, "skills")

	dir, _, err := InstallSkillArchive(archive, skillsDir, "my-skill")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if want := filepath.Join(skillsDir, "my-skill"); dir != want {
		t.Fatalf("dir = %q, want %q", dir, want)
	}
}

// Unlike authoring, installing from the store deliberately replaces.
func TestInstallSkillArchiveReplacesExisting(t *testing.T) {
	tmp := t.TempDir()
	skillsDir := filepath.Join(tmp, "skills")
	stale := filepath.Join(skillsDir, "x", "OLD.md")
	if err := os.MkdirAll(filepath.Dir(stale), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stale, []byte("old"), 0644); err != nil {
		t.Fatal(err)
	}

	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{"x/SKILL.md": "new"})

	dir, _, err := InstallSkillArchive(archive, skillsDir, "")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Error("stale file survived the replace — install is not a clean swap")
	}
	if _, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		t.Errorf("new content missing: %v", err)
	}
	if _, err := os.Stat(dir + ".old"); !os.IsNotExist(err) {
		t.Error("backup dir was left behind")
	}
}

func TestInstallSkillArchiveRejectsTraversal(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "evil.zip")
	// Two top-level segments so no wrapping dir is stripped; the ".." then has
	// to be caught by the per-entry guard.
	makeZip(t, archive, map[string]string{
		"SKILL.md":      "ok",
		"../escaped.md": "pwned",
	})
	skillsDir := filepath.Join(tmp, "skills")

	if _, _, err := InstallSkillArchive(archive, skillsDir, "x"); err == nil {
		t.Fatal("expected traversal entry to be rejected")
	}
	if _, err := os.Stat(filepath.Join(tmp, "escaped.md")); !os.IsNotExist(err) {
		t.Error("traversal entry escaped the skills dir")
	}
	if _, err := os.Stat(filepath.Join(skillsDir, "x")); !os.IsNotExist(err) {
		t.Error("a failed install must leave nothing behind")
	}
}

func TestInstallSkillArchiveEmpty(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "empty.zip")
	makeZip(t, archive, map[string]string{})

	if _, _, err := InstallSkillArchive(archive, filepath.Join(tmp, "skills"), "x"); !errors.Is(err, ErrEmptyArchive) {
		t.Fatalf("err = %v, want ErrEmptyArchive", err)
	}
}
