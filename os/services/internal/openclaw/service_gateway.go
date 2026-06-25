package openclaw

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// gatewayRestartTimeout bounds a single `systemctl restart openclaw` (or the
// `openclaw gateway restart` fallback). systemd's restart is synchronous —
// without a bound, a gateway that fails to re-bind its socket would block the
// caller (which holds the connector writer mutex) indefinitely. Generous enough
// for a healthy Pi restart (~30-60s) plus margin.
const gatewayRestartTimeout = 90 * time.Second

func generateGatewayToken() (string, error) {
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

func (s *OpenclawService) onboardOpenclaw() error {
	// openclaw default home is ~/.openclaw; OpenclawConfigDir must match this path.
	// No env overrides needed — let openclaw use its standard paths.
	cmd := exec.Command("bash", "-c", "openclaw onboard --non-interactive --accept-risk")
	out, err := cmd.CombinedOutput()
	if err != nil {
		outStr := strings.TrimSpace(string(out))
		// Factory reset disables openclaw.service so the gateway is not running when
		// onboard executes. In this case onboard writes the config successfully but
		// fails its end-of-run health check. Detect by the --skip-health hint in the
		// output and retry without the health check, then re-enable the service.
		if strings.Contains(outStr, "skip-health") {
			slog.Warn("onboard health check failed (gateway not running), retrying with --skip-health",
				"component", "openclaw", "output", outStr)
			out2, err2 := exec.Command("bash", "-c", "openclaw onboard --non-interactive --accept-risk --skip-health").CombinedOutput()
			if err2 != nil {
				return fmt.Errorf("openclaw onboard --skip-health: %w — output: %s", err2, strings.TrimSpace(string(out2)))
			}
			// Re-enable openclaw.service so it auto-starts on future reboots.
			if _, pathErr := exec.LookPath("systemctl"); pathErr == nil {
				if out3, err3 := exec.Command("systemctl", "enable", "openclaw").CombinedOutput(); err3 != nil {
					slog.Warn("re-enable openclaw.service failed", "component", "openclaw",
						"error", err3, "output", strings.TrimSpace(string(out3)))
				} else {
					slog.Info("openclaw.service re-enabled after factory reset onboard", "component", "openclaw")
				}
			}
		} else {
			return fmt.Errorf("openclaw onboard: %w — output: %s", err, outStr)
		}
	}

	// After onboard, ensure openclaw.json points workspace to our config dir's workspace.
	// Since OpenclawConfigDir matches openclaw's default home (~/.openclaw), the workspace
	// is already at the correct path; we only patch the field to be explicit.
	configPath := fmt.Sprintf("%s/openclaw.json", s.config.OpenclawConfigDir)
	workspacePath := fmt.Sprintf("%s/workspace", s.config.OpenclawConfigDir)
	if configBytes, err := os.ReadFile(configPath); err == nil {
		var configData map[string]interface{}
		if err := json.Unmarshal(configBytes, &configData); err == nil {
			agentsMap, ok := configData["agents"].(map[string]interface{})
			if !ok {
				agentsMap = make(map[string]interface{})
				configData["agents"] = agentsMap
			}
			defaultsMap, ok := agentsMap["defaults"].(map[string]interface{})
			if !ok {
				defaultsMap = make(map[string]interface{})
				agentsMap["defaults"] = defaultsMap
			}
			defaultsMap["workspace"] = workspacePath
			// Remove "tailscale" section from gateway if present
			gateway, ok := configData["gateway"].(map[string]interface{})
			if ok {
				delete(gateway, "tailscale")
			}
			configData["gateway"] = gateway
			if outBytes, err := json.MarshalIndent(configData, "", "  "); err == nil {
				_ = os.WriteFile(configPath, outBytes, 0600)
			}
		}
	}

	return nil
}

func restartOpenclawGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayRestartTimeout)
	defer cancel()

	if os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			out, err := exec.CommandContext(ctx, "systemctl", "restart", "openclaw").CombinedOutput()
			if err == nil {
				return nil
			}
			slog.Warn("systemctl restart failed, fallback", "component", "openclaw", "output", strings.TrimSpace(string(out)))
		}
	}
	out, err := exec.CommandContext(ctx, "openclaw", "gateway", "restart").CombinedOutput()
	if err == nil {
		return nil
	}
	output := strings.TrimSpace(string(out))
	lower := strings.ToLower(output)
	if strings.Contains(lower, "systemd user services are unavailable") ||
		strings.Contains(lower, "run the gateway in the foreground") {
		slog.Warn("no supported service manager, skip restart", "component", "openclaw", "output", output)
		return nil
	}
	return fmt.Errorf("openclaw gateway restart: %w - output: %s", err, output)
}
