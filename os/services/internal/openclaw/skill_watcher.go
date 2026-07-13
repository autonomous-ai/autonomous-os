package openclaw

import (
	"context"
	"fmt"
	"log/slog"
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

// downloadSkillsByName installs specific skill zips into the shared store and
// returns the names whose content actually changed. The download/extract/hash
// work is no longer OpenClaw's: every runtime downloading the same zips into its
// own dir is what let two devices on one release end up with different
// connectors/SKILL.md. One store, one copy, one version.
func (s *OpenclawService) downloadSkillsByName(names []string) []string {
	return skills.InstallByName(s.skillsBaseURL(), names)
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
