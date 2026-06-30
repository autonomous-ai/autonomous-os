package server

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/safego"
	"go.autonomous.ai/os/server/config"
	_sensingHttpDeliver "go.autonomous.ai/os/server/sensing/delivery/http"
)

// runConfigChangeListener listens for config changes and calls handleSetUpCompleteChange only when SetUpCompleted changed.
func (s *Server) runConfigChangeListener(ctx context.Context) {
	ch := s.config.GetNotifyChannel()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ch:
			// Refresh the HAL bearer token whenever config changes — covers
			// llm_api_key rotation via PUT /api/device/config without restart.
			hal.SetAPIKey(s.config.LLMAPIKey)
			s.handleSetUpCompleteChange(s.config.SetUpCompleted)
			s.handleDeviceIDChange(s.config.DeviceID)
			s.handleMQTTConfigChange()
		}
	}
}

// handleDeviceIDChange restarts claude-desktop-buddy when device_id changes. Buddy's
// BLE name is now derived from the device id (Claude-{MAC}, e.g. Claude-lamp-a1b2) so the
// restart isn't needed for name resolution, but a device_id transition is
// still a useful signal that the device has been re-provisioned — restarting
// buddy clears any stale BLE pairing state from the previous identity.
//
// On the first call (startup bootstrap) we just record the current value
// without restarting — only later transitions trigger a restart.
//
// Best-effort: if claude-desktop-buddy isn't installed (systemctl returns non-zero) we
// log and move on.
func (s *Server) handleDeviceIDChange(deviceID string) {
	if s.lastDeviceID == nil {
		s.lastDeviceID = &deviceID
		return
	}
	if *s.lastDeviceID == deviceID {
		return
	}
	prev := *s.lastDeviceID
	s.lastDeviceID = &deviceID

	slog.Info("device_id changed, restarting claude-desktop-buddy", "component", "config", "old", prev, "new", deviceID)
	safego.Go("claude-desktop-buddy-restart", func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()

		// Skip silently if claude-desktop-buddy isn't installed on this Pi. `systemctl cat`
		// exits non-zero when the unit doesn't exist; that's expected on devices
		// without the buddy plugin and we don't want to spam logs there.
		if err := exec.CommandContext(ctx, "systemctl", "cat", "claude-desktop-buddy.service").Run(); err != nil {
			return
		}

		out, err := exec.CommandContext(ctx, "systemctl", "restart", "claude-desktop-buddy").CombinedOutput()
		if err != nil {
			slog.Warn("claude-desktop-buddy restart failed", "component", "config", "error", err, "output", strings.TrimSpace(string(out)))
			return
		}
		slog.Info("claude-desktop-buddy restarted", "component", "config")
	})
}

// handleMQTTConfigChange restarts the MQTT client when ANY broker-connection
// field changes (endpoint, port, username, password, or the subscribed
// fa_channel) — whether pushed by the backend (status-reporter ping response) or
// edited via PUT /api/device/config — so the new config is picked up without a
// full device restart. restartMQTT reconnects and re-subscribes to fa_channel.
//
// On the first call (startup bootstrap) we just record the current signature
// without restarting — handleSetUpCompleteChange already brings MQTT up on the
// initial setup-completed flip, so we only need to act on later changes.
func (s *Server) handleMQTTConfigChange() {
	sig := fmt.Sprintf("%s|%d|%s|%s|%s",
		s.config.MQTTEndpoint, s.config.MQTTPort, s.config.MQTTUsername,
		s.config.MQTTPassword, s.config.FAChannel)
	if s.lastMQTTSig == nil {
		s.lastMQTTSig = &sig
		return
	}
	if *s.lastMQTTSig == sig {
		return
	}
	s.lastMQTTSig = &sig

	slog.Info("mqtt config changed, restarting mqtt client", "component", "config", "endpoint", s.config.MQTTEndpoint)
	s.restartMQTT()
}

// waitAndPaintSetupReady polls HAL /health up to 30s; when LED hardware
// reports ready it paints the strip solid white as the "device awaiting WiFi
// setup" cue. Exits early if setup completes mid-wait so we don't repaint
// over the post-setup user/agent LED state. Best-effort — silent when HAL
// never reports LED ready within budget (logs a warning).
//
// Why this is a poll loop and not a single fire-and-forget call: os-server binds
// :5000 faster than HAL's FastAPI binds :5001 on cold boot, so a fire-
// and-forget paint at L<see Serve> would silently drop on connection refused
// and leave the strip dark — exactly when the user needs the "ready for AP"
// signal most.
func (s *Server) waitAndPaintSetupReady() {
	deadline := time.Now().Add(30 * time.Second)
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for time.Now().Before(deadline) {
		if s.config.SetUpCompleted {
			return
		}
		if h, err := hal.GetHealth(); err == nil && h.LED {
			hal.SetStatus("setup")
			slog.Info("setup-needed white painted", "component", "server")
			return
		}
		<-ticker.C
	}
	slog.Warn("setup-needed paint skipped: hal LED not ready within 30s", "component", "server")
}

// handleSetUpCompleteChange starts or stops the network monitor and status reporter based on SetUpCompleted.
// When true: cancels any previous monitor context, creates a new one, starts monitor and reporter, and runs OpenClaw ready check.
// When false: cancels monitor/reporter (they exit on ctx.Done()) and switches to AP mode.
func (s *Server) handleSetUpCompleteChange(setupCompleted bool) {
	if s.lastSetupCompleted != nil && *s.lastSetupCompleted == setupCompleted {
		return
	}
	if setupCompleted {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
		}
		s.monitorCtx, s.monitorCancel = context.WithCancel(context.Background())
		s.monitorMu.Unlock()

		slog.Info("setup completed, starting internet monitor", "component", "config")
		s.networkService.StartNetworkMonitor(s.monitorCtx,
			func() { s.statusLED.Set(statusled.StateConnectivity) },
			func() { s.statusLED.Clear(statusled.StateConnectivity) },
		)
		slog.Info("setup completed, starting status reporter", "component", "config")
		safego.Go("status-reporter", func() { s.deviceService.StartStatusReporter(s.monitorCtx) })

		// Keep Google (Workspace) access tokens fresh: they expire after 1 hour
		// and the device holds only the refresh_token, so the actual exchange
		// runs on the backend. Loop refreshes them before they lapse.
		safego.Go("oauth-refresh", func() { s.deviceMQTTHandler.StartOAuthRefreshLoop(s.monitorCtx) })

		// Keep MCP connector tokens (Notion/Figma/Asana/Linear/GitHub) fresh.
		// Same model as oauth-refresh: only entries the backend flagged
		// refresh:true (with a refresh_token) are rotated before they lapse.
		safego.Go("connector-refresh", func() { s.deviceMQTTHandler.StartConnectorRefreshLoop(s.monitorCtx) })

		s.restartMQTT()

		safego.Go("startup-sequence", func() {
			// Migrate persona/memory if agent runtime switched; non-blocking.
			s.personaMigration.Reconcile()

			// Re-apply configured messaging channels to the (possibly new) runtime
			// and record any the runtime can't run. No-op when the runtime is
			// unchanged; gated by config.ChannelsAppliedRuntime. Non-blocking.
			s.channelReconcile.Reconcile()

			// Clone the previous runtime's MCP connectors into the (possibly new)
			// runtime so wired connectors survive a switch. No-op when the runtime is
			// unchanged; gated by config.MCPAppliedRuntime. Non-blocking.
			s.mcpReconcile.Reconcile()

			// Seed SOUL.md + IDENTITY.md into workspace (factory defaults, once only)
			if err := s.agentGateway.EnsureOnboarding(); err != nil {
				slog.Error("onboarding seed failed", "component", "server", "error", err)
			}

			// Start the periodic model sync only AFTER onboarding finishes —
			// both touch openclaw.json (ensureAgentDefaults via os.WriteFile,
			// sync via atomic tmp+rename); running them concurrently would
			// race and could clobber sync's writes.
			safego.Go("model-sync", func() { s.agentGateway.StartModelSync(s.monitorCtx) })
			safego.Go("primary-model-watch", func() { s.agentGateway.StartPrimaryModelWatch(s.monitorCtx) })

			if ok := s.deviceService.WaitForAgentReady(120 * time.Second); ok {
				slog.Info("agent gateway ready", "component", "server")
				s.statusLED.FlashReady()
			} else {
				slog.Warn("agent gateway ready timeout", "component", "server")
			}
			// Restart hal only when the config it reads changed since HAL last
			// started — e.g. fresh setup, an OTA config swap, or an edit while
			// os-server was down. A plain os-server restart with unchanged config
			// leaves the already-running HAL untouched, so we don't needlessly drop
			// the voice pipeline. If HAL is actually down, hal.service Restart=always
			// brings it back independently.
			if config.HALConfigChanged() {
				slog.Info("config changed since HAL last started, restarting hal", "component", "server")
				if out, err := exec.Command("systemctl", "restart", "hal").CombinedOutput(); err != nil {
					slog.Warn("hal restart failed", "component", "server", "error", err, "output", string(out))
				} else if err := config.SnapshotHALConfig(); err != nil {
					slog.Warn("hal config snapshot failed", "component", "server", "error", err)
				}
			} else {
				slog.Info("config unchanged since HAL last started, skipping hal restart", "component", "server")
			}
			// Start voice pipeline on HAL (if Deepgram key configured)
			// Retry because hal may not be running yet at setup time.
			if s.config.DeepgramAPIKey != "" {
				for attempt := 1; attempt <= 10; attempt++ {
					err := s.agentGateway.StartHALVoice(s.config.DeepgramAPIKey, s.config.LLMAPIKey, s.config.GetSTTAPIKey(), s.config.GetTTSAPIKey(), s.config.LLMBaseURL, s.config.GetSTTBaseURL(), s.config.GetTTSBaseURL(), s.config.TTSVoice, s.config.TTSInstructions, s.config.TTSProvider)
					if err == nil {
						break
					}
					slog.Warn("start HAL voice failed", "component", "server", "attempt", attempt, "maxAttempts", 10, "error", err)
					time.Sleep(5 * time.Second)
				}
			}

			// Init speaker volume from the device profile (DEVICE.md `startup_volume`,
			// default 100). 100 keeps the legacy behavior — software at max so the
			// hardware/alsactl level is the effective control — while a device with a
			// loud speaker can declare a quieter boot level instead of hardcoding it.
			startupVol := device.StartupVolume(s.config.DeviceTypeOrDefault())
			if err := s.agentGateway.SetVolume(startupVol); err != nil {
				slog.Warn("init volume failed", "component", "server", "error", err, "volume", startupVol)
			}

			// Greet user now that agent + voice pipeline are ready.
			// Prompt is localized by STTLanguage so the very first turn
			// lands in the owner's language without relying on the agent
			// to translate the priming message.
			slog.Info("INBOUND from system → agent (startup greeting)",
				"component", "server", "backend", s.agentGateway.Name(),
				"source", "wake_greeting")
			if _, err := s.agentGateway.SendSystemChatMessage(wakeGreetingPrompt()); err != nil {
				slog.Warn("startup greeting failed", "component", "server", "backend", s.agentGateway.Name(), "error", err)
			}

			// Prewarm dead-air filler WAV cache so the first filler fire is
			// a cache hit (~50ms) instead of a 1.5s ElevenLabs roundtrip.
			// Runs in a goroutine because rendering ~17 phrases serially can
			// take 30-60s and must not block the boot greeting.
			safego.Go("prewarm-fillers", func() { _sensingHttpDeliver.PrewarmFillers() })
			// Start ambient life behaviors (breathing LED, micro-movements, mumbles)
			safego.Go("ambient", func() { s.ambientService.Start(s.monitorCtx) })
			// Watch HAL component health; auto-restart voice on ALSA failure
			safego.Go("healthwatch", func() { s.healthWatch.Start(s.monitorCtx) })
		})
	} else {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
			s.monitorCancel = nil
		}
		s.monitorMu.Unlock()
		s.stopMQTT()
		s.networkService.SwitchToAPMode()
	}
	s.lastSetupCompleted = &setupCompleted
}
