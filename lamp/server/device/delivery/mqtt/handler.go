package mqtthandler

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
	"go-lamp.autonomous.ai/internal/network"
	"go-lamp.autonomous.ai/internal/openclaw"
	"go-lamp.autonomous.ai/lib/mqtt"
	"go-lamp.autonomous.ai/server/config"
)

// DeviceMQTTHandler handles incoming MQTT messages and dispatches to command handlers.
type DeviceMQTTHandler struct {
	config         *config.Config
	mqttFactory    *mqtt.Factory
	deviceService  *device.Service
	networkService *network.Service
	agentGateway   domain.AgentGateway
	// connectorWriters routes connector.set/remove (and the refresh loop) to a
	// per-connector writer; built once here, never mutated at runtime.
	connectorWriters connectorWriterRegistry
}

// mcpConnectorSpec lists the remote-MCP connectors that get a dedicated writer.
// apiKey:true selects the static-API-key header builder (Ahrefs); the rest use
// the default OAuth Bearer access-token builder. The MCP URL is pulled from the
// openclaw catalog so there is a single source of truth.
var mcpConnectorSpecs = []struct {
	name   string
	apiKey bool
}{
	{name: "notion"},
	{name: "figma"},
	{name: "asana"},
	{name: "linear"},
	{name: "github"},
	{name: "ahrefs", apiKey: true},
}

// googleOAuthConnectors lists the connectors that clone the Google OAuth
// credential model instead of the remote-MCP model. They are real connectors
// (connector.set.<code>/connector.remove.<code>) and refresh through the same
// connector refresh loop, but they write NO mcp.servers.<code> entry into
// openclaw.json — there is no hosted per-service Google MCP URL to point at.
// Each gets its own <code>_access_tokens.json via oauthConnectorWriter.
var googleOAuthConnectors = []string{"gmail", "google_calendar", "google_drive"}

// buildConnectorWriters constructs the writer registry: a generic
// connectors.json writer under "default" plus one mcpConnectorWriter per
// known remote-MCP connector.
func buildConnectorWriters(cfg *config.Config, gw domain.AgentGateway) connectorWriterRegistry {
	configsDir := filepath.Join(cfg.OpenclawConfigDir, "workspace", "configs")
	reg := connectorWriterRegistry{
		"default": newDefaultConnectorWriter(configsDir),
	}
	for _, sp := range mcpConnectorSpecs {
		url, ok := openclaw.MCPConnectorURL(sp.name)
		if !ok {
			continue
		}
		mcfg := mcpConnectorConfig{name: sp.name, mcpURL: url}
		if sp.apiKey {
			mcfg.header = bearerAPIKey
		}
		reg[sp.name] = newMCPConnectorWriter(mcfg, configsDir, gw)
	}
	// Google OAuth connectors: per-connector token file, no openclaw.json entry.
	for _, name := range googleOAuthConnectors {
		reg[name] = newOAuthConnectorWriter(name, configsDir)
	}

	// figma-api: REST variant. Instead of the hosted Figma MCP (allowlist-gated),
	// wire a local stdio MCP server that wraps the Figma REST API. The wrapper
	// script is dropped on disk; the access token rides in the entry's env.
	wrapperPath := openclaw.FigmaMCPServerPath(cfg.OpenclawConfigDir)
	ocDir := cfg.OpenclawConfigDir
	reg["figma-api"] = newMCPConnectorWriter(mcpConnectorConfig{
		name: "figma-api",
		entry: func(c ConnectorCreds) map[string]any {
			return map[string]any{
				"command": "node",
				"args":    []any{wrapperPath},
				"env":     map[string]any{"FIGMA_ACCESS_TOKEN": c.AccessToken},
			}
		},
		ensureAssets: func() error {
			// Wrapper script is required — fail the connector.set if it can't drop.
			if _, err := openclaw.EnsureFigmaMCPServer(ocDir); err != nil {
				return err
			}
			// Skill (SKILL.md from GCS) is best-effort: the figma_* tools still
			// work via the MCP tool list without it. Idempotent — only downloads
			// when missing, so token refreshes don't re-fetch.
			if err := openclaw.EnsureMCPSkill(ocDir, "figma-api"); err != nil {
				slog.Warn("figma-api: skill install failed (continuing)", "component", "mqtt", "error", err)
			}
			return nil
		},
	}, configsDir, gw)

	return reg
}

// ProvideDeviceMQTTHandler creates DeviceMQTTHandler with all command handlers.
func ProvideDeviceMQTTHandler(cfg *config.Config, mqttFactory *mqtt.Factory, ds *device.Service, ns *network.Service, gw domain.AgentGateway) DeviceMQTTHandler {
	return DeviceMQTTHandler{
		config:           cfg,
		mqttFactory:      mqttFactory,
		deviceService:    ds,
		networkService:   ns,
		agentGateway:     gw,
		connectorWriters: buildConnectorWriters(cfg, gw),
	}
}

func (h *DeviceMQTTHandler) publish(data interface{}) error {
	ctx := context.Background()
	mqttClient := h.mqttFactory.GetClient("lamp-device-" + h.config.DeviceID)
	if err := mqttClient.Connect(ctx); err != nil {
		return err
	}
	defer mqttClient.Close()
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	if err := mqttClient.Publish(ctx, h.config.FDChannel, byte(0), payload); err != nil {
		slog.Error("PublishToFD failed", "component", "mqtt", "channel", h.config.FDChannel, "error", err)
		return err
	}
	slog.Debug("PublishToFD ok", "component", "mqtt", "channel", h.config.FDChannel, "payload", string(payload))
	return nil
}

// handleData routes a generic cmd:"data" envelope by its delivery Type:
//
//   - default (Type == "")  → Data is inline; dispatch immediately.
//   - Type == "privacy"     → Data lives on the backend; ack "received" then
//     async-fetch it over TLS before re-entering dispatchData with Data
//     populated (see privacy_fetch.go).
//
// Per-kind handlers don't care which path the data took — they read env.Data
// after the routing layer has populated it.
func (h *DeviceMQTTHandler) handleData(cmd domain.MQTTMessage) error {
	var env domain.MQTTDataCommand
	if err := json.Unmarshal(cmd.Raw(), &env); err != nil {
		slog.Error("data: invalid envelope", "component", "mqtt", "error", err)
		return h.publishDataResult("", "failure", "invalid envelope: "+err.Error(), nil)
	}

	if env.Type == domain.MQTTDataTypePrivacy {
		return h.handlePrivacyEnvelope(env)
	}
	return h.dispatchData(env)
}

// dispatchData is the per-kind switch, shared by the inline and privacy paths.
// New sub-handlers go in this switch — adding one does NOT require touching the
// privacy fetch flow, which only cares about env.Kind/env.Type.
func (h *DeviceMQTTHandler) dispatchData(env domain.MQTTDataCommand) error {
	// Connector kinds carry the connector code as suffix (e.g.
	// "connector.set.notion"), so prefix-match before the exact-kind switch.
	if strings.HasPrefix(env.Kind, domain.DataKindConnectorSetPrefix) {
		return h.handleConnectorSet(env)
	}
	if strings.HasPrefix(env.Kind, domain.DataKindConnectorRemovePrefix) {
		return h.handleConnectorRemove(env)
	}
	switch env.Kind {
	case domain.KindTTSSet:
		return h.handleTTSSet(env)
	case domain.KindTTSPreview:
		return h.handleTTSPreview(env)
	case domain.KindLampRename:
		return h.handleLampRename(env)
	case domain.KindOAuthSet:
		return h.handleOAuthSet(env)
	case domain.KindOAuthRemove:
		return h.handleOAuthRemove(env)
	case domain.KindSystemInfo:
		return h.handleSystemInfo(env)
	case domain.KindSystemVersion:
		return h.handleSystemVersion(env)
	case domain.KindSystemNetwork:
		return h.handleSystemNetwork(env)
	case domain.KindSkillsInstall:
		return h.handleSkillsInstall(env)
	default:
		slog.Warn("unknown data kind", "component", "mqtt", "kind", env.Kind)
		return h.publishDataResult(env.Kind, "failure", "unknown kind: "+env.Kind, nil)
	}
}

// HandleMessage processes an incoming MQTT message (called from MQTT subscription callback or GWS HTTP).
func (h *DeviceMQTTHandler) HandleMessage(topic string, payload []byte) error {
	slog.Debug("HandleMessage", "component", "mqtt", "topic", topic, "payload", string(payload))

	var cmd domain.MQTTMessage
	if err := json.Unmarshal(payload, &cmd); err != nil {
		slog.Error("invalid payload", "component", "mqtt", "error", err)
		return fmt.Errorf("unmarshal mqtt command: %w", err)
	}

	switch cmd.Cmd {
	case domain.CommandInfo:
		return h.handleInfo(cmd)
	case domain.CommandAddChannel:
		return h.handleAddChannel(cmd)
	case domain.CommandSlackEvent:
		return h.handleSlackEvent(cmd)
	case domain.CommandWhatsappPair:
		return h.handleWhatsappPair(cmd)
	case domain.CommandData:
		return h.handleData(cmd)
	default:
		slog.Warn("unknown command", "component", "mqtt", "cmd", cmd.Cmd)
		return nil
	}
}
