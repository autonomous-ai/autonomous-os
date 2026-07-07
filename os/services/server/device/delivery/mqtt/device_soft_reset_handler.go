package mqtthandler

import (
	"log/slog"
	"os"
	"os/exec"

	"go.autonomous.ai/os/domain"
)

// configPath is the on-disk location of the device's config.json — same relative
// path config.Load() reads (os-server runs with WorkingDirectory=/root per the
// systemd unit, so this resolves to /root/config/config.json). Hard-coded here
// rather than sourced from the config package because the config package's copy
// is unexported and re-exporting it just for one caller would leak setup-time
// storage details into the public API.
const configPath = "config/config.json"

// handleDeviceSoftReset wipes the on-disk config and restarts os-server so the
// device drops back into AP setup mode WITHOUT rebooting or rolling back the
// firmware. Mirrors the manual `rm /root/config/config.json && systemctl restart
// os-server` flow we've been using for setup-flow testing.
//
// Fire-and-forget by design: no ack is published back, and no delay guards the
// tear-down. We tried both a 1s and a 5s delay + `publishDataResult("received")`
// before, and the ack still routinely lost the race — the device's networking
// dies faster than the frame can round-trip device → broker → BFF → browser,
// so the web client kept seeing "Couldn't reach your device" even on successful
// resets. The web flow is now optimistic: it shows success as soon as the POST
// leaves the browser and asks the operator to click "Fire and rehire" to open
// setup themselves. That removes any dependency on the ack, so we skip
// publishing it here and act on the wipe/restart immediately.
//
// Failure modes:
//   - config.json missing: os.Remove returns ENOENT. Treated as success (device
//     is already in the "no config" state, restart still fires).
//   - systemctl not on PATH: swallow the error and os.Exit(0) as a fallback —
//     systemd Restart=always still brings us back into AP mode.
func (h *DeviceMQTTHandler) handleDeviceSoftReset(_ domain.MQTTDataCommand) error {
	slog.Info("device.soft_reset: received — wiping config + restarting", "component", "mqtt")

	go func() {
		if err := os.Remove(configPath); err != nil && !os.IsNotExist(err) {
			slog.Error("device.soft_reset: remove config failed", "component", "mqtt", "path", configPath, "error", err)
			// Continue to restart anyway — device may still enter AP mode if
			// config parsing fails at load, and refusing to restart on a
			// remove error would strand the caller.
		} else {
			slog.Info("device.soft_reset: config wiped", "component", "mqtt", "path", configPath)
		}
		slog.Info("device.soft_reset: restarting os-server", "component", "mqtt")
		if err := exec.Command("systemctl", "restart", "os-server").Run(); err != nil {
			slog.Error("device.soft_reset: systemctl restart failed, falling back to os.Exit", "component", "mqtt", "error", err)
			os.Exit(0)
		}
	}()

	return nil
}
