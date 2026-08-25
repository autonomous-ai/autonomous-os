package bootstrap

import (
	"context"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// ComponentVersion is one row of GET /versions: what this device runs, what the
// feed offers, and whether the worker would actually apply it.
type ComponentVersion struct {
	Current    string `json:"current"`
	Target     string `json:"target"`
	MinVersion string `json:"min_version"`
	// UpdateAvailable is true when the feed offers a build NEWER than what runs
	// here. It says nothing about the rollout floor — a newer build can be
	// published and still held back (see HeldByFloor).
	UpdateAvailable bool `json:"update_available"`
	// HeldByFloor is true when a newer build exists but min_version has not been
	// promoted to it, i.e. the automatic worker will not apply it. A manual
	// force-check ignores the floor... in the sense that it runs the same
	// reconcile — which also respects it. So a held component only moves once
	// the floor is promoted; surfacing this keeps the UI from offering a button
	// that would do nothing.
	HeldByFloor bool `json:"held_by_floor"`
}

// versionReport answers "what can this device update right now", per component.
//
// Only components this device actually HAS are reported (componentInstalled),
// so the agent-CLI entry is the runtime the device runs and nothing else — the
// caller does not have to know which runtime that is. Metadata is fetched live:
// the worker keeps no cache, and this endpoint is hit by a human opening a page,
// not on a hot path.
func (b *Bootstrap) versionReport(ctx context.Context) map[string]ComponentVersion {
	out := map[string]ComponentVersion{}
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		slog.Warn("version report: metadata fetch failed", "component", "bootstrap", "error", err)
		return out
	}
	for _, key := range []string{
		domain.OTAKeyOSServer, domain.OTAKeyBootstrap, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy,
		domain.OTAKeyOpenClaw, domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw,
	} {
		component, ok := meta[key]
		if !ok || !b.componentInstalled(key) {
			continue
		}
		current := b.detectVersion(ctx, key)
		if current == "" {
			current = b.state.Components[key]
		}
		target := strings.TrimSpace(component.Version)
		minVersion := strings.TrimSpace(component.MinVersion)
		if minVersion == "" {
			minVersion = target
		}
		newer := compareVersions(current, target) < 0
		out[key] = ComponentVersion{
			Current:         current,
			Target:          target,
			MinVersion:      minVersion,
			UpdateAvailable: newer,
			HeldByFloor:     newer && compareVersions(current, minVersion) >= 0,
		}
	}
	return out
}
