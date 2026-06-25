package picoclaw

import (
	_ "embed"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/runtimereg"
)

// InstallScript is the device-side installer for the PicoClaw backend, embedded in
// os-server so it ships + OTA-updates with the binary (no CDN round-trip needed).
// os-server materializes it to disk and switch-runtime runs it the first time a
// device switches to picoclaw. See install.sh for the contract (downloads the
// picoclaw binary, runs `picoclaw onboard`, writes + starts picoclaw.service,
// drops the verify hook, drops + runs runtime-picoclaw-presync) and
// docs/agentic/picoclaw.md.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for PicoClaw. os-server
// materializes it to /usr/local/bin/runtime-picoclaw-presync on every switch, and
// switch-runtime runs it right before picoclaw starts. It OWNS the model wiring
// (config.json agents.defaults + model_list, .security.yml api_keys) and the
// channel wiring (config.json channel_list + .security.yml channel tokens), plus a
// guarded `picoclaw migrate --workspace-only --force` that carries persona/memory/skills over from
// OpenClaw when workspace/skills is empty — so the config self-heals on every
// switch, including after a factory reset. Materializing it from os-server (not
// from install.sh, which only re-runs on a first install / failed verify) is what
// makes a plain os-server OTA refresh it on disk. See presync.sh.
//
//go:embed presync.sh
var PresyncScript []byte

// Register the embedded installer + presync so internal/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimePicoclaw, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimePicoclaw, PresyncScript)
}
