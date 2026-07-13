package picoclaw

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/internal/skills"
)

// A device whose workspace skills dir is a real directory keeps a private second copy
// of every skill, which is how two devices on one release drifted apart. Assert the dir
// ends up as a symlink to the store, and that a device already linked is left alone
// (a spurious "changed" would restart the gateway on every boot).
func TestEnsureSkillsLink_PointsPicoclawDirAtStore(t *testing.T) {
	oldWorkspace := picoclawWorkspaceDir
	picoclawWorkspaceDir = filepath.Join(t.TempDir(), "workspace")
	t.Cleanup(func() { picoclawWorkspaceDir = oldWorkspace })

	store := skills.SetDirForTest(t, filepath.Join(t.TempDir(), "store"))

	if !ensureSkillsLink() {
		t.Fatal("ensureSkillsLink() = false on first call, want true (dir was not yet linked)")
	}

	skillsDir := filepath.Join(picoclawWorkspaceDir, "skills")
	target, err := os.Readlink(skillsDir)
	if err != nil {
		t.Fatalf("readlink %s: %v", skillsDir, err)
	}
	if target != store {
		t.Fatalf("skills dir points at %q, want the shared store %q", target, store)
	}

	if ensureSkillsLink() {
		t.Fatal("ensureSkillsLink() = true on second call, want false (already linked, must be idempotent)")
	}
}
