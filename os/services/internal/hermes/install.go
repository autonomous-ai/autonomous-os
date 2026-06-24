package hermes

import (
	_ "embed"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/runtimereg"
)

// InstallScript is the device-side installer for the Hermes backend, embedded in
// os-server so it ships + OTA-updates with the binary (no CDN round-trip needed).
// os-server materializes it to disk and switch-runtime runs it the first time a
// device switches to hermes. See install.sh for the contract (installs the
// gateway as hermes-gateway.service, declares that unit name for switch-runtime,
// drops + runs runtime-hermes-presync) and docs/agentic/hermes.md §10–§11.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for Hermes. os-server
// materializes it to /usr/local/bin/runtime-hermes-presync on every switch, and
// switch-runtime runs it right before hermes starts. It OWNS config.yaml's model
// wiring — the static provider structure plus the dynamic llm_*/channel sync from
// config.json — so the config self-heals on every switch, including after a
// factory reset wipes it. Materializing it from os-server (not from install.sh,
// which only re-runs on a first install / failed verify) is what makes a plain
// os-server OTA refresh it on disk. See presync.sh.
//
//go:embed presync.sh
var PresyncScript []byte

// Register the embedded installer + presync so internal/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeHermes, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeHermes, PresyncScript)
}
