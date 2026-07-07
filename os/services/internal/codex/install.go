package codex

import (
	_ "embed"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/runtimereg"
)

// InstallScript is the device-side installer for the Codex backend, embedded in
// os-server so it ships + OTA-updates with the binary. os-server materializes
// it to disk and switch-runtime runs it the first time a device switches to
// codex. See install.sh for the contract (downloads the pinned codex CLI
// release, writes + starts codex.service running `os-server codex-gatewayd`,
// drops the verify hook, runs runtime-codex-presync) and docs/agentic/codex.md.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for Codex. os-server
// materializes it to /usr/local/bin/runtime-codex-presync on every switch, and
// switch-runtime runs it right before codex starts (EnsureOnboarding also runs
// it every boot). It OWNS config.toml (model/provider/approval wiring,
// preserving the [mcp_servers] tail mcp.go edits), the .env file (llm_* from
// config.json), and the one-shot persona/memory migration from OpenClaw. The
// WS bridge itself is compiled into os-server (gatewayd package) — nothing to
// materialize. See presync.sh.
//
//go:embed presync.sh
var PresyncScript []byte

// Register the embedded installer + presync so internal/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeCodex, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeCodex, PresyncScript)
}
