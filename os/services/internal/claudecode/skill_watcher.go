package claudecode

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/skills"
)

const skillWatchInterval = 5 * time.Minute

// StartSkillWatcher polls OTA metadata for per-skill version changes and auto-updates
// the workspace skills from the CDN. Mirrors internal/openclaw/skill_watcher.go: the
// CDN fetch / atomic extract / content-hash plumbing is runtime-agnostic and lives in
// internal/skills; this file holds only the claudecode-specific loop, target dir, and
// notify. Capability-gated so a CDN version bump never re-adds a skill this device
// type does not support.
func (s *ClaudeCodeService) StartSkillWatcher(ctx context.Context) {
	slog.Info("skill watcher started", "component", "skill-watcher", "backend", "Claude Code", "interval", skillWatchInterval)

	// Seed last known versions so the first poll doesn't re-notify.
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

// supportedSkills resolves this device's capabilities from DEVICE.md and filters the
// platform skill catalog to what it can run (shared metadata in internal/skills, the
// same gate OpenClaw uses).
func (s *ClaudeCodeService) supportedSkills() []string {
	return skills.Supported(device.Capabilities(s.config.DeviceTypeOrDefault()))
}

// otaBaseURL derives the CDN base from the device's OTA metadata URL
// (`<base>/ota/metadata.json`). Returns "" when unset so callers skip rather than
// fall back to a hardcoded URL. Mirrors openclaw.
func (s *ClaudeCodeService) otaBaseURL() string {
	u := strings.TrimSpace(s.config.OTAMetadataURL)
	if u == "" {
		return ""
	}
	return strings.TrimSuffix(u, "/ota/metadata.json")
}

func (s *ClaudeCodeService) skillsBaseURL() string {
	if base := s.otaBaseURL(); base != "" {
		return base + "/skills"
	}
	return ""
}

// downloadSkillsByName installs the named skills into the SHARED store
// (internal/skills). Claude Code reads them through the ~/.claude/skills symlink
// ensureSkillsLink maintains, so every runtime sees the same copy — the per-runtime
// copies this replaced drifted between devices. Returns the names that changed.
func (s *ClaudeCodeService) downloadSkillsByName(names []string) []string {
	return skills.InstallByName(s.skillsBaseURL(), names)
}

// notifySkillChanges tells the agent to re-read the changed skills. Mirrors openclaw.
func (s *ClaudeCodeService) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	list := ""
	for _, name := range changedSkills {
		list += fmt.Sprintf("\n- %s/%s/SKILL.md", claudecodeSkillsDir, name)
	}
	msg := fmt.Sprintf("[system] The following skills have been updated. Re-read them now — files on disk have changed. Follow the updated instructions strictly. Keep your reply under 5 words.%s", list)
	slog.Info("INBOUND from system → agent (skill update)",
		"component", "skill-watcher", "backend", "Claude Code", "source", "skill_watcher", "changed", changedSkills)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}
