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
// drops + runs runtime-hermes-presync) and docs/hermes.md §10–§11.
//
//go:embed install.sh
var InstallScript []byte

// Register the embedded installer so internal/device can materialize it without
// importing this package (which would cycle via statusled → device).
func init() { runtimereg.Register(domain.AgentRuntimeHermes, InstallScript) }
