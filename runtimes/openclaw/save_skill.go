package openclaw

import (
	"log/slog"
	"path/filepath"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// SaveSkill writes a user-authored skill into {OpenclawConfigDir}/workspace/skills
// as <name>/SKILL.md — the same tree InstallRoleSkills extracts role bundles
// into, so an authored skill sits alongside store- and OTA-installed ones and
// is addressed the same way.
//
// The gateway is NOT restarted: skills.load.watch (set at setup, see
// service_setup.go) picks new files up per session, exactly as it does for
// InstallRoleSkills and EnsureMCPSkill.
func (s *OpenclawService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(s.skillsDir(), draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}

	slog.Info("[skills] authored skill saved", "component", "openclaw", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the same
// workspace/skills tree, replacing any existing skill of that name.
func (s *OpenclawService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, s.skillsDir(), fallbackName)
	if err != nil {
		return "", err
	}

	slog.Info("[skills] archive installed", "component", "openclaw", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in workspace/skills —
// authored, store-installed, role-bundled and OTA-pushed skills alike, since
// they all land in the same tree.
func (s *OpenclawService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(s.skillsDir())
}

// skillsDir is the tree OpenClaw loads skills from — the same one
// InstallRoleSkills and EnsureMCPSkill write into.
func (s *OpenclawService) skillsDir() string {
	return filepath.Join(s.config.OpenclawConfigDir, "workspace", "skills")
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *OpenclawService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(s.skillsDir(), name)
}
