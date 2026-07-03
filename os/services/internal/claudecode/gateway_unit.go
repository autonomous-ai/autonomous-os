package claudecode

import (
	"log/slog"
	"os"
	"os/exec"
	"strings"
)

// Systemd self-heal for the claudecode bridge unit. A device that reached
// claudecode WITHOUT switch-runtime (e.g. a hand-edited config.json
// agent_runtime=claudecode) has no unit, so IsReady()'s WS connect — and the
// setup WaitForAgentReady gate — would fail forever. EnsureOnboarding installs
// it on demand (the bridge itself is presync-materialized, so unit + bridge is
// all a hand-switched device needs). Mirrors hermes.ensureGatewayUnit.

const claudecodeUnitName = "claudecode"
const claudecodeUnitPath = "/etc/systemd/system/claudecode.service"

// claudecodeUnitContent MUST stay in sync with the unit install.sh writes —
// two writers, one contract (cross-referenced in install.sh).
const claudecodeUnitContent = `[Unit]
Description=Claude Code agent bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
WorkingDirectory=/root/.claudecode
ExecStart=/usr/bin/python3 /root/.claudecode/bridge.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
`

// ensureGatewayUnit writes the systemd unit when it is missing. Returns true
// when it was installed this call (EnsureOnboarding then restarts). No-op on a
// non-root / no-systemctl box (dev machine).
func (s *ClaudeCodeService) ensureGatewayUnit() bool {
	if os.Geteuid() != 0 {
		return false
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return false
	}
	if _, err := os.Stat(claudecodeUnitPath); err == nil {
		return false // unit present
	}
	if err := os.WriteFile(claudecodeUnitPath, []byte(claudecodeUnitContent), 0o644); err != nil {
		slog.Warn("write claudecode unit failed", "component", "claudecode", "error", err)
		return false
	}
	if out, err := exec.Command("systemctl", "daemon-reload").CombinedOutput(); err != nil {
		slog.Warn("systemctl daemon-reload failed", "component", "claudecode",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
	slog.Info("claudecode unit installed (self-heal)", "component", "claudecode", "path", claudecodeUnitPath)
	return true
}

// gatewayActive reports whether the claudecode unit is currently active. On a
// box without systemctl it returns true so EnsureOnboarding does not loop on
// restarts it cannot perform.
func gatewayActive() bool {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return true
	}
	return exec.Command("systemctl", "is-active", "--quiet", claudecodeUnitName).Run() == nil
}

// enableClaudeCodeGateway re-enables the unit so the bridge survives a reboot —
// factory reset disables it, and a freshly self-healed unit is not enabled.
// Best-effort.
func enableClaudeCodeGateway() {
	if os.Geteuid() != 0 {
		return
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return
	}
	if out, err := exec.Command("systemctl", "enable", claudecodeUnitName).CombinedOutput(); err != nil {
		slog.Warn("systemctl enable claudecode failed", "component", "claudecode",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
}
