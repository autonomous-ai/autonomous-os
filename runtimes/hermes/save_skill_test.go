package hermes

import "testing"

// The device-owned root must stay OUT of openclaw-imports: presync.sh §0
// restores imported skills by running `claw migrate` only when that dir is
// EMPTY, so an authored skill in there would make the guard permanently think
// the imports are present and a factory reset would never restore them.
func TestAuthoredSkillsRootIsSeparateFromImports(t *testing.T) {
	if hermesAuthoredSkillsDir == hermesImportedSkillsDir {
		t.Fatal("authored skills must not share the migrate-owned imports root")
	}
	if want := hermesHome + "/skills/authored"; hermesAuthoredSkillsDir != want {
		t.Errorf("authored root = %q, want %q", hermesAuthoredSkillsDir, want)
	}
	if want := hermesHome + "/skills/openclaw-imports"; hermesImportedSkillsDir != want {
		t.Errorf("imports root = %q, want %q", hermesImportedSkillsDir, want)
	}
}
