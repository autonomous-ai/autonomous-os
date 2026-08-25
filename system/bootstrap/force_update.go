package bootstrap

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

// forceUpdate installs the component's published `version` NOW, exactly like
// running `software-update <key>` on the device — which is what the web
// Versions card's button stands for.
//
// It deliberately does NOT go through reconcile: reconcile implements the
// AUTOMATIC rollout, which only moves a device up to the approved floor
// (min_version). The floor is a fleet-wide staging control, not a restriction on
// an operator who is deliberately updating one device — the SSH command has
// always ignored it, and the button is the same act performed from the UI.
//
// Still gated by componentInstalled: a device must not be pushed a component it
// does not have (e.g. the agent CLI of a runtime it does not run), no matter who
// asks.
func (b *Bootstrap) forceUpdate(ctx context.Context, key string) error {
	if !b.componentInstalled(key) {
		return fmt.Errorf("%s is not installed on this device", key)
	}

	// Same operator-visible cues as an automatic update: speak once, breathe
	// orange, then green on success / red on failure.
	b.announceUpdateStart()
	b.progressLED("ota_progress")

	// applyUpdate ignores the component argument for every key that delegates to
	// `software-update` (the script self-fetches metadata and installs
	// `version`), so there is nothing to look up here.
	if err := b.applyUpdate(ctx, key, domain.OTAComponent{}); err != nil {
		b.progressLED("ota_error")
		return err
	}
	b.progressLED("ota_success")
	time.Sleep(time.Second)
	b.restoreLED()

	// Record what actually landed rather than what was requested: the script may
	// have installed a version the feed moved on from, and for hermes it cannot
	// be pinned at all.
	if installed := b.detectVersion(ctx, key); installed != "" {
		b.state.Components[key] = installed
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			slog.Warn("force update: save state failed", "component", "bootstrap", "key", key, "error", err)
		}
	}
	slog.Info("force update applied", "component", "bootstrap", "key", key)
	return nil
}
