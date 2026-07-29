package opencode

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for OpenCode. Mirrors
// runtimes/openclaw/save_skill.go — the rendering, extraction and tree walk are
// shared in system/skills; only the target directory differs per backend.
//
// That directory is opencode's native discovery root (`$XDG_CONFIG_HOME/opencode/skills`), the
// same tree migrateSkillsToOpenCodeHome moves legacy workspace skills into.
//
// The runtime is NOT restarted for any of these: OpenCode discovers skills per
// session, the same contract the skill watcher relies on.

// SaveSkill writes a user-authored skill as <name>/SKILL.md.
func (s *OpenCodeService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(opencodeSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "opencode", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the skills
// dir, replacing any existing skill of that name.
func (s *OpenCodeService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, opencodeSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "opencode", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in the skills dir.
func (s *OpenCodeService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(opencodeSkillsDir)
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *OpenCodeService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(opencodeSkillsDir, name)
}
