package device

import (
	"context"
	"encoding/json"
	"log/slog"
	"strconv"
	"time"

	"go.autonomous.ai/os/system/beclient"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/runtimereg"
	"go.autonomous.ai/os/system/server/config"
)

// buildPingPayload assembles the backend ping body with the same device-state
// fields the MQTT `info` uplink publishes (local_ip, versions, runtime,
// voice/STT, timezone) so the backend can read them from either channel. The
// critical field is LocalIP: the setup web popup's parent page polls the
// backend for it to rescue the AP→STA redirect when both the AP-alive window
// and mDNS fail (see docs/setup-flow.md). Every field is omitempty — a backend
// that consumes none of them loses nothing.
func (s *Service) buildPingPayload(status string) beclient.PingPayload {
	runtime := CurrentAgentRuntimeFromConfig(s.config)
	p := beclient.PingPayload{
		Status:              status,
		SetupCompleted:      s.config.SetUpCompleted,
		Mac:                 GetDeviceMac(),
		Version:             config.OSVersion,
		Device:              s.config.DeviceTypeOrDefault(),
		DeviceID:            s.config.DeviceID,
		Timezone:            s.CurrentTimezone(),
		AgentRuntime:        runtime,
		AgentRuntimeVersion: runtimereg.Version(runtime),
		TTSProvider:         s.config.TTSProvider,
		TTSVoice:            s.config.TTSVoice,
		STTLanguage:         s.config.STTLanguage,
		UnsupportedChannels: s.config.ChannelsUnsupported,
	}
	if ip, err := s.networkService.GetCurrentIP(); err == nil && ip != apSetupIP {
		p.LocalIP = ip
	}
	if v, err := hal.GetVersion(); err == nil {
		p.HalVersion = v
	}
	if s.beClient != nil {
		p.SlackTeamID = s.beClient.SlackTeamID()
	}
	return p
}

// StartStatusReporter periodically pings the autonomous backend.
// Uses LLMAPIKey as Bearer token. Exits when ctx is cancelled.
// If the backend response contains MQTT config, it saves to config (triggers config notify).
func (s *Service) StartStatusReporter(ctx context.Context) {
	if s.beClient == nil || s.config.LLMAPIKey == "" {
		return
	}
	ticker := time.NewTicker(beclient.StatusReportInterval)
	defer ticker.Stop()
	// Last LAN address we pinged from. The device's address is not stable: moving
	// the ethernet cable to another network, or a DHCP re-lease, changes it while
	// os-server keeps running.
	var lastLocalIP string
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if !s.agentGateway.IsReady() {
				continue
			}
			// Lazy resolve of slack team_id: once per process. ResolveSlackTeamIDFromConfig
			// is a no-op when team_id is already cached OR when slack isn't configured,
			// so it's safe to call on every tick — the auth.test call only fires once.
			s.beClient.ResolveSlackTeamIDFromConfig(s.config.OpenclawConfigDir)
			payload := s.buildPingPayload("working")
			// Address changed under us → every pooled connection to the backend is
			// bound to an address the device no longer holds. They don't fail fast:
			// the old path is gone, so there's no RST, and the next ping writes into
			// a blackhole until the 15s timeout. Drop them before pinging so this
			// tick dials fresh instead of burning a cycle (and logging a failure)
			// per stale connection.
			if payload.LocalIP != "" && lastLocalIP != "" && payload.LocalIP != lastLocalIP {
				slog.Info("local IP changed, dropping pooled backend connections",
					"component", "status-reporter", "from", lastLocalIP, "to", payload.LocalIP)
				s.beClient.CloseIdleConnections()
			}
			if payload.LocalIP != "" {
				lastLocalIP = payload.LocalIP
			}
			resp := s.beClient.PingSafe(s.config.LLMAPIKey, payload)
			dump, _ := json.Marshal(resp)
			slog.Debug("received response from backend", "component", "status-reporter", "response", string(dump))
			if resp == nil {
				continue
			}
			if resp.DeviceID != "" && resp.DeviceID != s.config.DeviceID {
				s.config.DeviceID = resp.DeviceID
			}
			if resp.HasMQTT() && resp.GetMQTT().Endpoint != s.config.MQTTEndpoint {
				mqttCfg := resp.GetMQTT()
				slog.Info("received MQTT config from backend", "component", "status-reporter", "endpoint", mqttCfg.Endpoint)
				s.config.MQTTEndpoint = mqttCfg.Endpoint
				port, _ := strconv.Atoi(mqttCfg.Port)
				s.config.MQTTPort = port
				s.config.MQTTUsername = mqttCfg.Username
				s.config.MQTTPassword = mqttCfg.Password
				s.config.FAChannel = mqttCfg.FaChannel
				s.config.FDChannel = mqttCfg.FdChannel
				if err := s.config.Save(); err != nil {
					slog.Error("save MQTT config failed", "component", "status-reporter", "error", err)
				}
			}
		}
	}
}
