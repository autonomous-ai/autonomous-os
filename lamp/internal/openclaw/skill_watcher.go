package openclaw

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const skillWatchInterval = 5 * time.Minute
const defaultOTAMetadataURL = "https://storage.googleapis.com/s3-autonomous-upgrade-3/lamp/ota/metadata.json"

// StartSkillWatcher polls OTA metadata for per-skill version changes.
// When any skill version changes, downloads that skill zip from CDN,
// extracts atomically, and notifies the agent to re-read it.
func (s *Service) StartSkillWatcher(ctx context.Context) {

	slog.Info("skill watcher started", "component", "skill-watcher", "interval", skillWatchInterval)

	// Seed last known versions from current metadata so first poll doesn't re-notify
	lastVersions := map[string]string{}
	if initial, err := s.fetchSkillVersions(); err == nil && initial != nil {
		lastVersions = initial
		slog.Info("skill watcher seeded versions", "component", "skill-watcher", "count", len(lastVersions))
	}

	ticker := time.NewTicker(skillWatchInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("skill watcher stopped", "component", "skill-watcher")
			return
		case <-ticker.C:
			remote, err := s.fetchSkillVersions()
			if err != nil {
				slog.Info("skill watcher: fetch failed", "component", "skill-watcher", "error", err)
				continue
			}
			slog.Info("skill watcher: checked", "component", "skill-watcher", "skills", len(remote))

			// Find skills with changed versions
			var toUpdate []string
			for name, ver := range remote {
				if ver != "" && ver != lastVersions[name] {
					toUpdate = append(toUpdate, name)
					lastVersions[name] = ver
				}
			}
			if len(toUpdate) == 0 {
				continue
			}

			slog.Info("skill versions changed", "component", "skill-watcher", "skills", toUpdate)
			changed := s.downloadSkillsByName(toUpdate)
			s.notifySkillChanges(changed)
		}
	}
}

// downloadSkills downloads all skills from CDN, returns names of changed ones.
func (s *Service) downloadSkills() []string {
	return s.downloadSkillsByName(skills)
}

// downloadSkillsByName downloads specific skill zips from CDN, extracts each
// atomically, returns names of skills that landed on disk successfully.
//
// Each skill is published as ``<name>.zip`` containing the entire skill folder
// (SKILL.md + any reference / example / sub-folder content). Atomic extract:
// download to temp, unzip into ``<target>.new/``, then swap with the live
// ``<target>/`` via os.Rename. Old contents are removed in the swap so files
// deleted in the new version don't linger. Caller (the watcher loop or
// onboarding) already pre-filters by version, so any successful return here
// means content actually changed.
func (s *Service) downloadSkillsByName(names []string) []string {
	skillsDir := filepath.Join(s.config.OpenclawConfigDir, "workspace", "skills")
	var changed []string
	for _, name := range names {
		url := fmt.Sprintf("%s/%s.zip", skillsBaseURL, name)
		tmpZip, err := downloadToTempFile(url, "skill-*.zip")
		if err != nil {
			slog.Warn("skill zip download failed", "component", "skill-watcher", "skill", name, "error", err)
			continue
		}

		targetDir := filepath.Join(skillsDir, name)

		// Hash existing content before extract so we can detect a no-op
		// update — metadata version bumped but actual files on disk
		// would land identical. Empty hash on first install or unreadable
		// dir is fine: any new content will differ.
		oldHash, _ := folderHash(targetDir)

		if err := extractSkillZip(tmpZip, targetDir); err != nil {
			slog.Warn("skill extract failed", "component", "skill-watcher", "skill", name, "error", err)
			os.Remove(tmpZip)
			continue
		}
		os.Remove(tmpZip)

		// Skip notifying the agent when the extracted content is byte-for-byte
		// identical to what was already on disk. Pre-2026-05-04 (per-file
		// SKILL.md) this was implicit because only changed files were fetched;
		// the multi-file zip path bumps the metadata version any time the
		// uploader re-runs, even when no skill content actually changed.
		newHash, _ := folderHash(targetDir)
		if oldHash != "" && oldHash == newHash {
			slog.Info("skill content unchanged after extract, skipping notify",
				"component", "skill-watcher", "skill", name)
			continue
		}
		changed = append(changed, name)
	}
	return changed
}

// folderHash computes a deterministic sha256 of dir's content tree (paths +
// file bytes, walked in lexical order). Returns "" if dir doesn't exist or
// can't be walked — caller treats empty as "no prior content".
func folderHash(dir string) (string, error) {
	h := sha256.New()
	err := filepath.Walk(dir, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		// Include relative path so file moves register as changes.
		h.Write([]byte(rel))
		h.Write([]byte{0})
		if info.IsDir() {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		if _, err := io.Copy(h, f); err != nil {
			return err
		}
		h.Write([]byte{0})
		return nil
	})
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// notifySkillChanges sends a single message to the agent listing all changed skills.
func (s *Service) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	slog.Info("skills updated, notifying agent", "component", "skill-watcher", "changed", changedSkills)
	list := ""
	for _, name := range changedSkills {
		list += fmt.Sprintf("\n- skills/%s/SKILL.md", name)
	}
	msg := fmt.Sprintf("[system] The following skills have been updated. Re-read them now — files on disk have changed. Follow the updated instructions strictly. Keep your reply under 5 words.%s", list)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}

// fetchSkillVersions gets per-skill versions from OTA metadata.
// Returns map[skillName]version.
func (s *Service) fetchSkillVersions() (map[string]string, error) {
	url := s.config.OTAMetadataURL
	if url == "" {
		url = defaultOTAMetadataURL
	}
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var meta map[string]json.RawMessage
	if err := json.Unmarshal(body, &meta); err != nil {
		return nil, err
	}
	raw, ok := meta["skills"]
	if !ok {
		return nil, nil
	}
	var skillMap map[string]struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal(raw, &skillMap); err != nil {
		return nil, err
	}
	result := make(map[string]string, len(skillMap))
	for name, v := range skillMap {
		result[name] = v.Version
	}
	return result, nil
}

// downloadToTempFile fetches url and writes to a temp file, returning its
// path. Caller must os.Remove the returned path when done.
func downloadToTempFile(url, pattern string) (string, error) {
	client := &http.Client{Timeout: 60 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Cache-Control", "no-cache")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	f, err := os.CreateTemp("", pattern)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		os.Remove(f.Name())
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(f.Name())
		return "", err
	}
	return f.Name(), nil
}

// extractSkillZip atomically replaces ``targetDir`` with the contents of
// ``archivePath``. Steps:
//  1. clean ``<targetDir>.new/``
//  2. unzip archive into it (path-traversal guarded)
//  3. on full success, remove ``targetDir`` and rename ``<targetDir>.new`` →
//     ``targetDir``
//
// Failure at any step leaves ``targetDir`` untouched, so a corrupt download
// can't blow away a working skill.
func extractSkillZip(archivePath, targetDir string) error {
	tmpDir := targetDir + ".new"
	if err := os.RemoveAll(tmpDir); err != nil {
		return fmt.Errorf("clean tmp dir: %w", err)
	}
	if err := os.MkdirAll(tmpDir, 0755); err != nil {
		return fmt.Errorf("mkdir tmp dir: %w", err)
	}

	if err := unzipInto(archivePath, tmpDir); err != nil {
		os.RemoveAll(tmpDir)
		return err
	}

	if err := os.RemoveAll(targetDir); err != nil {
		os.RemoveAll(tmpDir)
		return fmt.Errorf("remove old target: %w", err)
	}
	if err := os.Rename(tmpDir, targetDir); err != nil {
		// Last-ditch: try to recover the failed swap rather than leave
		// the skill missing entirely.
		_ = os.RemoveAll(tmpDir)
		return fmt.Errorf("rename %s → %s: %w", tmpDir, targetDir, err)
	}
	return nil
}

// unzipInto extracts every file in archivePath to dest with a path-traversal
// guard. Forces 0644 / 0755 perms (we don't trust modes from the upload host).
func unzipInto(archivePath, dest string) error {
	r, err := zip.OpenReader(archivePath)
	if err != nil {
		return fmt.Errorf("open zip %s: %w", archivePath, err)
	}
	defer r.Close()

	cleanDest, err := filepath.Abs(dest)
	if err != nil {
		return fmt.Errorf("abs dest: %w", err)
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	for _, f := range r.File {
		// Reject absolute / parent-traversing paths.
		if filepath.IsAbs(f.Name) || strings.Contains(f.Name, "..") {
			return fmt.Errorf("invalid zip entry %q", f.Name)
		}
		target := filepath.Join(dest, f.Name)
		// Belt-and-suspenders containment check after Join.
		absTarget, err := filepath.Abs(target)
		if err != nil {
			return fmt.Errorf("abs target %s: %w", target, err)
		}
		if !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) &&
			absTarget+string(os.PathSeparator) != cleanDest {
			return fmt.Errorf("zip entry escapes dest: %q", f.Name)
		}

		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0755); err != nil {
				return fmt.Errorf("mkdir %s: %w", target, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return fmt.Errorf("mkdir parent %s: %w", target, err)
		}

		rc, err := f.Open()
		if err != nil {
			return fmt.Errorf("open zip entry %s: %w", f.Name, err)
		}
		out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
		if err != nil {
			rc.Close()
			return fmt.Errorf("create %s: %w", target, err)
		}
		if _, err := io.Copy(out, rc); err != nil {
			rc.Close()
			out.Close()
			return fmt.Errorf("write %s: %w", target, err)
		}
		rc.Close()
		if err := out.Close(); err != nil {
			return fmt.Errorf("close %s: %w", target, err)
		}
	}
	return nil
}
