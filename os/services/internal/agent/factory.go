package agent

import (
	"log/slog"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/hermes"
	"go.autonomous.ai/os/internal/monitor"
	"go.autonomous.ai/os/internal/openclaw"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/server/config"
)

// ProvideGateway returns the AgentGateway implementation. The backend is chosen
// by config.AgentRuntime; when that is unset it falls back to the device's
// declared gateway.default (devices/<type>/DEVICE.md), then OpenClaw.
//
// "openclaw" (default): persistent WebSocket to the OpenClaw daemon at
// 127.0.0.1:18789. See internal/openclaw and docs/os-server.md.
//
// "hermes": HTTP+SSE client against the Hermes API server (default
// 127.0.0.1:8642). See internal/hermes and docs/hermes.md.
// gatewayTransport is the wire transport each runtime uses. The transport is a
// property of the runtime, not an independent knob, so DEVICE.md
// `gateway.protocol` is only validated against this (a consistency guard).
var gatewayTransport = map[string]string{
	"openclaw": "websocket",
	"hermes":   "sse",
}

func ProvideGateway(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) domain.AgentGateway {
	// Consistency guard: a device that declares gateway.protocol should match the
	// transport its gateway.default runtime actually speaks. Warn (don't fail) on
	// a contradiction — it can't drive anything, but it flags a misleading
	// DEVICE.md (e.g. default: hermes with protocol: websocket).
	devType := cfg.DeviceTypeOrDefault()
	if proto := device.GatewayProtocol(devType); proto != "" {
		if def := device.GatewayDefault(devType); def != "" {
			if want, ok := gatewayTransport[def]; ok && want != proto {
				slog.Warn("DEVICE.md gateway.protocol contradicts gateway.default's transport",
					"component", "agent", "device_type", devType,
					"gateway.default", def, "gateway.protocol", proto, "expected", want)
			}
		}
	}

	// Backend resolution: explicit config.agent_runtime wins; if unset, fall back
	// to the device's declared gateway.default (DEVICE.md); else OpenClaw default.
	runtime := cfg.AgentRuntime
	source := "config.agent_runtime"
	if runtime == "" {
		if g := device.GatewayDefault(devType); g != "" {
			runtime, source = g, "DEVICE.md gateway.default"
		}
	}

	switch runtime {
	case "hermes":
		logBackendBanner("HERMES", map[string]string{
			"base_url":     hermes.BaseURL,
			"conversation": hermes.Conversation,
			"model":        hermes.Model,
			"api_key_set":  boolStr(hermes.APIKey != ""),
			"source":       source,
		})
		return hermes.ProvideService(cfg, bus, sled)
	default:
		effective := runtime
		if effective == "" {
			effective = "openclaw (default — agent_runtime + gateway.default both unset)"
		} else if effective != "openclaw" {
			effective = "openclaw (FALLBACK — unknown runtime=" + runtime + ")"
		}
		logBackendBanner("OPENCLAW", map[string]string{
			"config_dir":      cfg.OpenclawConfigDir,
			"effective_value": effective,
			"source":          source,
		})
		return openclaw.ProvideService(cfg, bus, sled)
	}
}

func logBackendBanner(name string, fields map[string]string) {
	args := []any{"component", "agent", "backend", name}
	for k, v := range fields {
		args = append(args, k, v)
	}
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
	slog.Info("  AGENT BACKEND ACTIVE → "+name, args...)
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
}

func boolStr(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}
