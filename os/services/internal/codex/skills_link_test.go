package codex

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/internal/skills"
)

// ensureSkillsLink is what stops codex from keeping its own copy of the CDN skill
// zips (copies that drifted between devices, and left a duplicate of a skill name
// behind after a runtime switch). It must therefore leave workspace/skills as a
// symlink to the shared store, and must be safe to re-run on every boot.
func TestEnsureSkillsLink_PointsCodexDirAtStore(t *testing.T) {
	store := skills.SetDirForTest(t, filepath.Join(t.TempDir(), "store"))

	// Redirect the workspace off /root — a test cannot create dirs there.
	oldWorkspace := codexWorkspaceDir
	codexWorkspaceDir = filepath.Join(t.TempDir(), "workspace")
	t.Cleanup(func() { codexWorkspaceDir = oldWorkspace })

	if !ensureSkillsLink() {
		t.Fatal("ensureSkillsLink() = false on first call, want true (it had to create the link)")
	}

	skillsDir := filepath.Join(codexWorkspaceDir, "skills")
	target, err := os.Readlink(skillsDir)
	if err != nil {
		t.Fatalf("os.Readlink(%s): %v — codex skills dir is not a symlink", skillsDir, err)
	}
	if target != store {
		t.Errorf("skills dir points at %q, want the shared store %q", target, store)
	}

	// Idempotent: an already-linked dir is not a change, so a boot after the
	// migration must not report one and trigger a pointless gateway restart.
	if ensureSkillsLink() {
		t.Error("ensureSkillsLink() = true on second call, want false (already linked)")
	}
}
