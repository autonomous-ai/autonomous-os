package agent

import (
	"log/slog"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/server/config"
)

// UserProfileReconcile retires people from every runtime's USER.md once they no
// longer have a face/voice enrollment on this device.
//
// USER.md is a bootstrap file — injected into the agent's system prompt on every
// turn — yet nothing on the device ever updated it: the agent writes what it
// learns to KNOWLEDGE.md and memory/*.md, neither of which OpenClaw loads. The
// file that is always read was the one never written, so a device that changed
// hands kept greeting its previous owner by name (lamp-ac82, 2026-09-03).
//
// Absence is never the trigger — see migratepersona.ReconcileUserProfiles for
// the rule and its cost characteristics.
type UserProfileReconcile struct {
	opts    migratepersona.Options
	execute bool
}

// ProvideUserProfileReconcile builds the reconciler from device config.
//
// execute is gated on config so a device can be put in observe-only mode: the
// pass logs what it WOULD retire and writes nothing. The default (see
// config.UserProfileReconcileEnabled) is to apply — the fleet watched this pass
// observe-only for two weeks without a false retirement, and a `false` in
// config.json returns any device to observe-only.
func ProvideUserProfileReconcile(cfg *config.Config) *UserProfileReconcile {
	opts := migratepersona.DefaultOptions(cfg.OpenclawConfigDir, hermesHome)
	return &UserProfileReconcile{
		opts:    opts,
		execute: cfg.UserProfileReconcileEnabled(),
	}
}

// Reconcile runs one pass. Logs and never blocks startup, like the other
// startup reconcilers; a failure here must never keep the device from booting.
func (r *UserProfileReconcile) Reconcile() {
	actions, err := migratepersona.ReconcileUserProfiles(r.opts, r.execute)
	if err != nil {
		slog.Warn("user profile reconcile failed; profiles left untouched",
			"component", "user-reconcile", "error", err)
		return
	}
	if len(actions) == 0 {
		return // the normal case: nothing stale, nothing written, no cache miss
	}
	mode := "dry-run (agent.user_profile_reconcile=false)"
	if r.execute {
		mode = "applied"
	}
	for _, a := range actions {
		slog.Info("user profile reconcile", "component", "user-reconcile",
			"mode", mode, "path", a.Path, "kind", a.Kind,
			"detail", a.Detail, "reason", a.Reason)
	}
}
