package skills

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Installing a downloaded `.skill` archive into a runtime's skills dir.
//
// Like WriteAuthoredSkill, this lives here because only the TARGET DIRECTORY
// differs per agentic runtime — each backend's AgentGateway.InstallSkillArchive
// passes its own dir and this does the rest.
//
// Archive shape: the catalog's `.skill` bundles carry a single top-level
// `<name>/` directory (verified against the live catalog). Skill zips from the
// OTA CDN instead carry the files at the root. Both are handled: a single
// common top-level dir names the skill, otherwise fallbackName does.

// ErrEmptyArchive is returned when the archive holds no usable files.
var ErrEmptyArchive = errors.New("skill archive is empty")

// installMaxFiles / installMaxFileBytes bound what a single archive can write.
// Generous ceilings — they exist to stop a hostile or corrupt archive from
// filling the device, not to constrain real skills.
const (
	installMaxFiles     = 500
	installMaxFileBytes = 4 << 20
)

// InstallSkillArchive extracts archivePath into skillsDir as a single skill
// directory and returns the absolute path it wrote plus the file count.
//
// The write is atomic per skill: everything is staged under
// <skillsDir>/<name>.new and only swapped into place once the whole archive
// extracted cleanly, so a corrupt download can never leave a half-installed
// skill (or destroy a working one). An existing skill of that name IS replaced
// — installing from the store is an explicit user action, unlike authoring,
// which refuses to clobber.
//
// The runtime is NOT restarted; every backend with a skills dir picks new files
// up per session.
func InstallSkillArchive(archivePath, skillsDir, fallbackName string) (string, int, error) {
	if skillsDir == "" {
		return "", 0, errors.New("skills dir is not configured")
	}

	r, err := zip.OpenReader(archivePath)
	if err != nil {
		return "", 0, fmt.Errorf("open skill archive: %w", err)
	}
	defer r.Close()

	entries := usableEntries(r)
	if len(entries) == 0 {
		return "", 0, ErrEmptyArchive
	}
	if len(entries) > installMaxFiles {
		return "", 0, fmt.Errorf("skill archive has %d files (max %d)", len(entries), installMaxFiles)
	}

	// A single shared top-level dir names the skill and is stripped; otherwise
	// the files sit at the archive root and fallbackName names the skill.
	name, strip := archiveSkillName(entries)
	if name == "" {
		name = strings.TrimSpace(fallbackName)
	}
	if err := ValidateSkillName(name); err != nil {
		return "", 0, err
	}

	target := filepath.Join(skillsDir, name)
	staging := target + ".new"
	if err := os.RemoveAll(staging); err != nil {
		return "", 0, fmt.Errorf("clean staging dir: %w", err)
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		return "", 0, fmt.Errorf("create staging dir: %w", err)
	}

	count, err := extractEntries(entries, strip, staging)
	if err != nil {
		_ = os.RemoveAll(staging)
		return "", 0, err
	}
	if count == 0 {
		_ = os.RemoveAll(staging)
		return "", 0, ErrEmptyArchive
	}

	// Swap in. The old dir is moved aside first so a failed rename can be undone.
	backup := target + ".old"
	_ = os.RemoveAll(backup)
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, backup); err != nil {
			_ = os.RemoveAll(staging)
			return "", 0, fmt.Errorf("replace existing skill %s: %w", name, err)
		}
	}
	if err := os.Rename(staging, target); err != nil {
		_ = os.Rename(backup, target) // put the working skill back
		_ = os.RemoveAll(staging)
		return "", 0, fmt.Errorf("install skill %s: %w", name, err)
	}
	_ = os.RemoveAll(backup)

	return target, count, nil
}

// usableEntries drops directories and editor/OS cruft, and rejects nothing —
// path safety is enforced later, per entry, against the resolved staging dir.
func usableEntries(r *zip.ReadCloser) []*zip.File {
	out := make([]*zip.File, 0, len(r.File))
	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := filepath.ToSlash(f.Name)
		if strings.HasPrefix(name, "__MACOSX/") || filepath.Base(name) == ".DS_Store" {
			continue
		}
		out = append(out, f)
	}
	return out
}

// archiveSkillName returns the archive's single common top-level directory (and
// true, meaning that segment must be stripped on extract). Returns ("", false)
// when the files live at the archive root or span multiple top-level dirs.
func archiveSkillName(entries []*zip.File) (string, bool) {
	top := ""
	for _, f := range entries {
		name := filepath.ToSlash(f.Name)
		i := strings.Index(name, "/")
		if i <= 0 {
			return "", false // a file at the root — no single wrapping dir
		}
		seg := name[:i]
		if top == "" {
			top = seg
			continue
		}
		if seg != top {
			return "", false
		}
	}
	return top, top != ""
}

// extractEntries writes entries into dest, optionally stripping the first path
// segment. Path-traversal guarded against dest; forces 0644/0755 perms (modes
// from an upload host aren't trusted).
func extractEntries(entries []*zip.File, strip bool, dest string) (int, error) {
	cleanDest, err := filepath.Abs(dest)
	if err != nil {
		return 0, fmt.Errorf("resolve dest: %w", err)
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	count := 0
	for _, f := range entries {
		rel := filepath.ToSlash(f.Name)
		if strip {
			if i := strings.Index(rel, "/"); i >= 0 {
				rel = rel[i+1:]
			}
		}
		if rel == "" {
			continue
		}
		if strings.HasPrefix(rel, "/") || strings.Contains(rel, "..") {
			return count, fmt.Errorf("unsafe zip entry %q", f.Name)
		}

		target := filepath.Join(dest, filepath.FromSlash(rel))
		absTarget, err := filepath.Abs(target)
		if err != nil || !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) {
			return count, fmt.Errorf("zip entry escapes dest: %q", f.Name)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return count, fmt.Errorf("mkdir %s: %w", filepath.Dir(target), err)
		}
		if err := writeZipFile(f, target); err != nil {
			return count, err
		}
		count++
	}
	return count, nil
}

// copyLimited copies at most limit bytes and errors if the source has more —
// so a lying UncompressedSize64 (zip bomb) can't fill the disk.
func copyLimited(dst io.Writer, src io.Reader, limit int64) (int64, error) {
	n, err := io.Copy(dst, io.LimitReader(src, limit+1))
	if err != nil {
		return n, err
	}
	if n > limit {
		return n, fmt.Errorf("entry exceeds %d bytes", limit)
	}
	return n, nil
}

func writeZipFile(f *zip.File, target string) error {
	if f.UncompressedSize64 > installMaxFileBytes {
		return fmt.Errorf("zip entry %q is larger than %d bytes", f.Name, installMaxFileBytes)
	}
	rc, err := f.Open()
	if err != nil {
		return fmt.Errorf("open zip entry %s: %w", f.Name, err)
	}
	defer rc.Close()

	out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
	if err != nil {
		return fmt.Errorf("create %s: %w", target, err)
	}
	// LimitReader guards against a lying UncompressedSize64 (zip bomb).
	if _, err := copyLimited(out, rc, installMaxFileBytes); err != nil {
		out.Close()
		return fmt.Errorf("write %s: %w", target, err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("close %s: %w", target, err)
	}
	return nil
}
