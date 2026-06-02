package openclaw

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	// roleSkillsBaseURL is the GCS prefix holding one skills.zip per role. Each
	// zip contains a top-level `skills/` tree with every skill of the role
	// (e.g. skills/<name>/SKILL.md plus auxiliary files).
	roleSkillsBaseURL = "https://storage.googleapis.com/s3-autonomous-upgrade-3/plugins-skills/openclaw-roles"

	roleSkillsZipPrefix  = "skills/"
	roleSkillsMaxRetries = 3
	roleSkillsRetryDelay = 2 * time.Second
)

// ErrInvalidRole is returned when the role slug has an unsafe shape (empty or
// containing path-escaping characters). The set of valid roles is NOT
// hardcoded — the backend owns the catalog and the device fetches
// <role>/skills.zip on demand, so adding a role needs no code change. This
// guard only blocks path traversal / URL injection.
var ErrInvalidRole = errors.New("invalid role")

// roleNamePattern allows only lowercase letters, digits, dash and underscore.
var roleNamePattern = regexp.MustCompile(`^[a-z0-9_-]+$`)

// InstallRoleSkills downloads <role>/skills.zip from GCS and extracts its
// `skills/` tree into {configDir}/workspace/skills, returning the number of
// files written. Existing skills (other roles, OTA-pushed skills) are left
// untouched — only files present in the zip are (over)written, so installs are
// cumulative. The gateway is NOT restarted: skills.load.watch (set at setup,
// service_setup.go) picks new files up per session.
func InstallRoleSkills(configDir, role string) (int, error) {
	if !roleNamePattern.MatchString(role) {
		return 0, fmt.Errorf("%w: %q", ErrInvalidRole, role)
	}

	url := fmt.Sprintf("%s/%s/skills.zip", roleSkillsBaseURL, role)

	var tmpZip string
	var lastErr error
	for attempt := 1; attempt <= roleSkillsMaxRetries; attempt++ {
		p, err := downloadToTempFile(url, "role-skills-*.zip")
		if err != nil {
			lastErr = err
			slog.Warn("[role-skills] zip download failed", "component", "openclaw", "role", role, "attempt", attempt, "error", err)
			if attempt < roleSkillsMaxRetries {
				time.Sleep(roleSkillsRetryDelay)
			}
			continue
		}
		tmpZip = p
		lastErr = nil
		break
	}
	if lastErr != nil {
		return 0, fmt.Errorf("download %s skills zip after %d retries: %w", role, roleSkillsMaxRetries, lastErr)
	}
	defer os.Remove(tmpZip)

	skillsDir := filepath.Join(configDir, "workspace", "skills")
	count, err := extractDirFromZip(tmpZip, roleSkillsZipPrefix, skillsDir)
	if err != nil {
		return count, fmt.Errorf("extract %s skills: %w", role, err)
	}
	slog.Info("[role-skills] installed role", "component", "openclaw", "role", role, "files", count, "dir", skillsDir)
	return count, nil
}

// extractDirFromZip extracts every entry under srcPrefix in the zip at zipPath
// into destDir (with the prefix stripped), returning the number of files
// written. Path-traversal guarded; forces 0644/0755 perms. Cumulative — does
// not delete destDir first, so other roles' skills survive.
func extractDirFromZip(zipPath, srcPrefix, destDir string) (int, error) {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return 0, fmt.Errorf("open zip %s: %w", zipPath, err)
	}
	defer r.Close()

	cleanDest, err := filepath.Abs(destDir)
	if err != nil {
		return 0, fmt.Errorf("abs dest: %w", err)
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	count := 0
	for _, f := range r.File {
		if !strings.HasPrefix(f.Name, srcPrefix) {
			continue
		}
		rel := strings.TrimPrefix(f.Name, srcPrefix)
		if rel == "" {
			continue
		}
		if filepath.IsAbs(rel) || strings.Contains(rel, "..") {
			return count, fmt.Errorf("invalid zip entry %q", f.Name)
		}
		target := filepath.Join(destDir, rel)
		absTarget, err := filepath.Abs(target)
		if err != nil {
			return count, fmt.Errorf("abs target %s: %w", target, err)
		}
		if !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) &&
			absTarget+string(os.PathSeparator) != cleanDest {
			return count, fmt.Errorf("zip entry escapes dest: %q", f.Name)
		}

		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0755); err != nil {
				return count, fmt.Errorf("mkdir %s: %w", target, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return count, fmt.Errorf("mkdir parent %s: %w", target, err)
		}

		rc, err := f.Open()
		if err != nil {
			return count, fmt.Errorf("open zip entry %s: %w", f.Name, err)
		}
		out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
		if err != nil {
			rc.Close()
			return count, fmt.Errorf("create %s: %w", target, err)
		}
		if _, err := io.Copy(out, rc); err != nil {
			rc.Close()
			out.Close()
			return count, fmt.Errorf("write %s: %w", target, err)
		}
		rc.Close()
		if err := out.Close(); err != nil {
			return count, fmt.Errorf("close %s: %w", target, err)
		}
		count++
	}
	return count, nil
}
