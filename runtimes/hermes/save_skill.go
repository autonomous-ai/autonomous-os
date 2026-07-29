package hermes

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for Hermes. Mirrors
// runtimes/openclaw/save_skill.go — the rendering, extraction and tree walk are
// shared in system/skills; only the target directory differs per backend.
//
// Hermes is the one backend that NAMESPACES its skills dir, so it is also the
// one that needs more than a single path:
//
//	~/.hermes/skills/openclaw-imports/  ← owned by `hermes claw migrate`
//	~/.hermes/skills/authored/          ← owned by the device (this file)
//
// Device writes deliberately stay OUT of openclaw-imports. presync.sh §0
// restores the imported skills by running `claw migrate` *only when that dir is
// empty* — dropping an authored skill in there would make the dir look
// populated forever, so a factory reset would silently never restore the real
// platform skills. A separate root keeps that guard meaningful.
//
// Hermes discovers skills anywhere under ~/.hermes/skills (the SOUL.md
// skill-priority block addresses them by subpath, e.g. `skills/openclaw-imports/`),
// so the new root needs no config change. No gateway restart either — Hermes
// re-reads skills per session.
const (
	// hermesAuthoredSkillsDir is where everything os-server writes lands.
	hermesAuthoredSkillsDir = hermesHome + "/skills/authored"

	// hermesImportedSkillsDir is the migrate-owned root, read-only from here.
	hermesImportedSkillsDir = hermesHome + "/skills/openclaw-imports"
)

// SaveSkill writes a user-authored skill as <name>/SKILL.md under the
// device-owned root.
func (s *HermesService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(hermesAuthoredSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "hermes", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the
// device-owned root, replacing any existing skill of that name there. An
// imported skill of the same name in openclaw-imports is left alone — that root
// belongs to `claw migrate`.
func (s *HermesService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, hermesAuthoredSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "hermes", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills merges both roots. The device-owned root is scanned first so a
// skill the user created or installed is the one shown when an imported skill
// shares its name.
func (s *HermesService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalledFrom(hermesAuthoredSkillsDir, hermesImportedSkillsDir)
}

// ReadSkillFiles searches the device-owned root first, then the migrate-owned
// one — same precedence as ListSkills, so the detail view opens the same skill
// the listing showed.
func (s *HermesService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFilesFrom(name, hermesAuthoredSkillsDir, hermesImportedSkillsDir)
}
