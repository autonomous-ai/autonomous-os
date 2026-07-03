package claudecode

import (
	_ "embed"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/runtimereg"
)

// InstallScript is the device-side installer for the Claude Code backend, embedded in
// os-server so it ships + OTA-updates with the binary (no CDN round-trip needed).
// os-server materializes it to disk and switch-runtime runs it the first time a
// device switches to claudecode. See install.sh for the contract (installs the
// claude CLI + bun + telegram channel plugin + the python websockets dep, writes +
// starts claudecode.service, drops the verify hook, runs
// runtime-claudecode-presync) and docs/agentic/claudecode.md.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for Claude Code. os-server
// materializes it to /usr/local/bin/runtime-claudecode-presync on every switch,
// switch-runtime runs it right before claudecode starts, and EnsureOnboarding
// re-runs it on every os-server boot / config change (hermes-style). It OWNS the
// bridge (bridge.py heredoc), the launch env (/root/.claudecode/.env —
// ANTHROPIC_* from config.json llm_*), the headless flags (~/.claude.json,
// workspace settings.json), and the telegram channel config
// (~/.claude/channels/telegram/.env + access.json + the --channels launch flag)
// — so the config self-heals on every switch/boot, including after a factory
// reset. Persona/memory migration is NOT here: claudecode has a Go adapter
// (internal/agent/migrate_persona/runtime_claudecode.go). Materializing this
// from os-server (not from install.sh, which only re-runs on a first install /
// failed verify) is what makes a plain os-server OTA refresh it on disk.
//
//go:embed presync.sh
var PresyncScript []byte

// Register the embedded installer + presync so internal/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeClaudeCode, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeClaudeCode, PresyncScript)
}
