package openclaw

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"go.autonomous.ai/os/internal/skills"
)

const skillWatchInterval = 5 * time.Minute

// StartSkillWatcher polls OTA metadata for per-skill version changes.
// When any skill version changes, downloads that skill zip from CDN,
// extracts atomically, and notifies the agent to re-read it.
//
// The CDN fetch / atomic extract / content-hash plumbing is runtime-agnostic and
// lives in internal/skills (FetchSkillVersions / DownloadToTempFile / FolderHash /
// ExtractSkillZip); this file holds only the OpenClaw-specific loop, target dir,
// and notify. internal/hermes/skill_watcher.go is its parallel under Hermes.
func (s *OpenclawService) StartSkillWatcher(ctx context.Context) {

	slog.Info("skill watcher started", "component", "skill-watcher", "interval", skillWatchInterval)

	// Seed last known versions from current metadata so first poll doesn't re-notify
	lastVersions := map[string]string{}
	if initial, err := skills.FetchSkillVersions(s.config.OTAMetadataURL); err == nil && initial != nil {
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
			remote, err := skills.FetchSkillVersions(s.config.OTAMetadataURL)
			if err != nil {
				slog.Info("skill watcher: fetch failed", "component", "skill-watcher", "error", err)
				continue
			}
			slog.Info("skill watcher: checked", "component", "skill-watcher", "skills", len(remote))

			// Find skills with changed versions, gated to what this device
			// supports so a CDN version bump never re-adds a capability-pruned
			// skill (e.g. servo-control on a motionless device).
			supported := map[string]bool{}
			for _, n := range s.supportedSkills() {
				supported[n] = true
			}
			var toUpdate []string
			for name, ver := range remote {
				if !supported[name] {
					continue
				}
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

// downloadSkills downloads the skills this device supports from CDN (capability-
// gated via supportedSkills), returning names of changed ones.
func (s *OpenclawService) downloadSkills() []string {
	return s.downloadSkillsByName(s.supportedSkills())
}

// downloadSkillsByName downloads specific skill zips from CDN, extracts each
// atomically into workspace/skills/<name>, returns names of skills that actually
// changed on disk. Each skill is published as <name>.zip containing the whole
// skill folder; the version pre-filter + content hash mean a returned name had
// real content changes.
func (s *OpenclawService) downloadSkillsByName(names []string) []string {
	base := s.skillsBaseURL()
	if base == "" {
		slog.Info("skill download skipped: no ota_metadata_url configured", "component", "skill-watcher")
		return nil
	}
	skillsDir := filepath.Join(s.config.OpenclawConfigDir, "workspace", "skills")
	var changed []string
	for _, name := range names {
		url := fmt.Sprintf("%s/%s.zip", base, name)
		tmpZip, err := skills.DownloadToTempFile(url, "skill-*.zip")
		if err != nil {
			slog.Warn("skill zip download failed", "component", "skill-watcher", "skill", name, "error", err)
			continue
		}

		targetDir := filepath.Join(skillsDir, name)

		// Hash existing content before extract so we can detect a no-op update —
		// metadata version bumped but actual files would land identical.
		oldHash, _ := skills.FolderHash(targetDir)

		if err := skills.ExtractSkillZip(tmpZip, targetDir); err != nil {
			slog.Warn("skill extract failed", "component", "skill-watcher", "skill", name, "error", err)
			os.Remove(tmpZip)
			continue
		}
		os.Remove(tmpZip)

		newHash, _ := skills.FolderHash(targetDir)
		if oldHash != "" && oldHash == newHash {
			slog.Info("skill content unchanged after extract, skipping notify",
				"component", "skill-watcher", "skill", name)
			continue
		}
		changed = append(changed, name)
	}
	return changed
}

// notifySkillChanges sends a single message to the agent listing all changed skills.
func (s *OpenclawService) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	list := ""
	for _, name := range changedSkills {
		list += fmt.Sprintf("\n- skills/%s/SKILL.md", name)
	}
	msg := fmt.Sprintf("[system] The following skills have been updated. Re-read them now — files on disk have changed. Follow the updated instructions strictly. Keep your reply under 5 words.%s", list)
	slog.Info("INBOUND from system → agent (skill update)",
		"component", "skill-watcher", "backend", "OpenClaw",
		"source", "skill_watcher", "changed", changedSkills)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}
