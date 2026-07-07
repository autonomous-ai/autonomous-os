package hermes

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUpsertSoulSkillPriorityBlock_AppendsBelowPersona(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n"
	got := upsertSoulSkillPriorityBlock(in)
	if !strings.HasPrefix(got, "# Soul\n\nYou are Lamp.\n") {
		t.Fatalf("persona content not preserved at top:\n%q", got)
	}
	if !strings.Contains(got, soulOSMarker) {
		t.Fatalf("marker missing:\n%q", got)
	}
	if !strings.Contains(got, "`connectors` skill") {
		t.Fatalf("connectors rule missing:\n%q", got)
	}
	if !strings.HasSuffix(got, "---\n") {
		t.Fatalf("block must close with --- separator:\n%q", got)
	}
}

func TestUpsertSoulSkillPriorityBlock_Idempotent(t *testing.T) {
	once := upsertSoulSkillPriorityBlock("# Soul\n\npersona\n")
	twice := upsertSoulSkillPriorityBlock(once)
	if once != twice {
		t.Fatalf("not idempotent:\n once=%q\ntwice=%q", once, twice)
	}
}

func TestUpsertSoulSkillPriorityBlock_ReplacesStaleBlock(t *testing.T) {
	stale := "# Soul\n\npersona\n\n" + soulOSMarker + "\nOLD RULE\n---\n"
	got := upsertSoulSkillPriorityBlock(stale)
	if strings.Contains(got, "OLD RULE") {
		t.Fatalf("stale block content survived:\n%q", got)
	}
	if strings.Count(got, soulOSMarker) != 1 {
		t.Fatalf("want exactly one marker block, got %d:\n%q", strings.Count(got, soulOSMarker), got)
	}
	if !strings.Contains(got, "persona") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

func TestUpsertSoulSkillPriorityBlock_EmptySoul(t *testing.T) {
	got := upsertSoulSkillPriorityBlock("")
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("empty soul should become just the block:\n%q", got)
	}
	if upsertSoulSkillPriorityBlock(got) != got {
		t.Fatalf("not idempotent on block-only soul")
	}
}

func TestUpsertSoulSkillPriorityBlock_PreservesIdentityCard(t *testing.T) {
	// The identity card (identity.go / persona migration) must survive the
	// strip+re-append — only the marker-delimited block is managed.
	in := "# Soul\n\n## Your identity card\n\n- **Name:** Ngân\n\n" + soulOSMarker + "\nold\n---\n"
	got := upsertSoulSkillPriorityBlock(in)
	if !strings.Contains(got, "- **Name:** Ngân") {
		t.Fatalf("identity card lost:\n%q", got)
	}
	if idx := strings.Index(got, soulOSMarker); idx < strings.Index(got, "identity card") {
		t.Fatalf("block should sit below the identity card:\n%q", got)
	}
}

func mkSkill(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte("# "+name), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPruneImportedDuplicates_RemovesWhenBaseExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	mkSkill(t, dir, "connectors-imported")
	mkSkill(t, dir, "voice")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors-imported")); !os.IsNotExist(err) {
		t.Fatalf("duplicate not removed")
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors", "SKILL.md")); err != nil {
		t.Fatalf("canonical copy lost: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "voice")); err != nil {
		t.Fatalf("unrelated skill touched: %v", err)
	}
}

func TestPruneImportedDuplicates_RenamesWhenOnlyImportedExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "standup-imported")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "standup", "SKILL.md")); err != nil {
		t.Fatalf("imported-only skill not renamed to canonical name: %v", err)
	}
}

func TestPruneImportedDuplicates_NoopCases(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	// plain file with the suffix must be ignored (not a skill dir)
	if err := os.WriteFile(filepath.Join(dir, "notes-imported"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := pruneImportedDuplicatesIn(dir); got != 0 {
		t.Fatalf("changed = %d, want 0", got)
	}
	if got := pruneImportedDuplicatesIn(filepath.Join(dir, "absent")); got != 0 {
		t.Fatalf("absent dir: changed = %d, want 0", got)
	}
}
