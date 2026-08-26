// Package device owns the device-level service: setup/provisioning (setup.go),
// messaging channels (channels.go), config updates (config_update.go), realtime
// voice config (realtime.go), agent-runtime switching (runtime.go), the backend
// status reporter (status_reporter.go), ROBOT.md parsing (devicemd.go),
// hardware identity (hardware.go), and timezone (timezone.go).
package device

import (
	"errors"
	"log/slog"
	"os/exec"
	"sync"
	"time"

	"go.autonomous.ai/os/system/beclient"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// ErrAgentRuntimeSwitchInProgress prevents concurrent switch-runtime invocations.
// The switcher owns systemd units and must run exclusively.
var ErrAgentRuntimeSwitchInProgress = errors.New("agent runtime switch already in progress")

type Service struct {
	config          *config.Config
	networkService  *network.Service
	agentGateway    domain.AgentGateway
	beClient        *beclient.Client
	statusLED       *statusled.Service
	setupState      setupState
	runtimeSwitchMu sync.Mutex
}

func ProvideService(config *config.Config, ns *network.Service, gw domain.AgentGateway, be *beclient.Client, sled *statusled.Service) *Service {
	SeedAgentRuntimeFromGateway(config)
	return &Service{
		config:         config,
		networkService: ns,
		agentGateway:   gw,
		beClient:       be,
		statusLED:      sled,
		setupState:     setupState{phase: SetupPhaseIdle},
	}
}

// restartHAL restarts hal in the background so it re-reads config.json — HAL
// reads the voice pipeline and realtime blocks at import, so a restart is the
// only way to apply them. After a successful restart the boot-time config
// baseline is refreshed so the next os-server restart sees the running HAL as
// up-to-date and skips a redundant restart.
func (s *Service) restartHAL(reason string) {
	go func() {
		slog.Info("restarting hal", "component", "device", "reason", reason)
		out, err := exec.Command("systemctl", "restart", "hal").CombinedOutput()
		if err != nil {
			slog.Warn("hal restart failed", "component", "device", "reason", reason, "error", err, "output", string(out))
			return
		}
		slog.Info("hal restarted", "component", "device", "reason", reason)
		if err := config.SnapshotHALConfig(); err != nil {
			slog.Warn("hal config snapshot failed", "component", "device", "error", err)
		}
	}()
}

// applyTTSConfig pushes a voice change into the running hal instead of
// restarting it. Falls back to a restart on failure: a voice that was saved but
// never reached hal is a worse outcome than the restart this avoids, because
// the device would keep speaking in the old voice with nothing to show for it.
func (s *Service) applyTTSConfig(c *config.Config) {
	go func() {
		if err := hal.ApplyTTSConfig(c.TTSProvider, c.TTSVoice, c.TTSAPIKey, c.TTSBaseURL); err != nil {
			slog.Warn("hal tts config apply failed, restarting instead",
				"component", "device", "error", err)
			s.restartHAL("voice config change")
			return
		}
		slog.Info("hal tts config applied live", "component", "device",
			"provider", c.TTSProvider, "voice", c.TTSVoice)
		// Keep the boot-time baseline in step, or the next os-server start
		// would see drift and order a restart that is no longer needed.
		if err := config.SnapshotHALConfig(); err != nil {
			slog.Warn("hal config snapshot failed", "component", "device", "error", err)
		}
	}()
}

// WaitForAgentReady polls agentGateway.IsReady until it returns true or the timeout elapses.
func (s *Service) WaitForAgentReady(timeout time.Duration) bool {
	return waitForAgentReady(s.agentGateway, timeout, 0, 500*time.Millisecond)
}

// WaitForAgentReadyStable requires IsReady to remain true for stableFor before
// succeeding. Startup reconciliation can restart a runtime after an earlier
// health probe has marked it ready; a single true observation would then allow
// a startup greeting to race the restart and be dropped.
func (s *Service) WaitForAgentReadyStable(timeout, stableFor time.Duration) bool {
	return waitForAgentReady(s.agentGateway, timeout, stableFor, 500*time.Millisecond)
}

type agentReadiness interface {
	IsReady() bool
}

func waitForAgentReady(gateway agentReadiness, timeout, stableFor, pollInterval time.Duration) bool {
	if gateway == nil {
		return false
	}
	deadline := time.Now().Add(timeout)
	var readySince time.Time
	for {
		now := time.Now()
		if gateway.IsReady() {
			if readySince.IsZero() {
				readySince = now
			}
			if now.Sub(readySince) >= stableFor {
				return true
			}
		} else {
			readySince = time.Time{}
		}
		if !now.Before(deadline) {
			return false
		}
		time.Sleep(pollInterval)
	}
}
