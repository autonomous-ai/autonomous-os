package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go-lamp.autonomous.ai/domain"
)

// defaultModels is the hardcoded list of supported models.
var defaultModels = []domain.LLMModel{
	{
		Key:       "claude-opus-4-6",
		Name:      "claude-opus-4-6",
		Reasoning: true,
		Input:     []string{"text", "image"},
		Privacy:   "private",
		Capabilities: &domain.LLMModelCapabilities{
			SupportsReasoning:       true,
			SupportsVision:          true,
			SupportsFunctionCalling: true,
		},
	},
	{
		Key:       "claude-haiku-4-5",
		Name:      "claude-haiku-4-5",
		Reasoning: true,
		Input:     []string{"text", "image"},
		Privacy:   "private",
		Capabilities: &domain.LLMModelCapabilities{
			SupportsReasoning:       true,
			SupportsVision:          true,
			SupportsFunctionCalling: true,
		},
	},
}

// listModelsFromAPI returns the hardcoded default models list.
func (s *Service) listModelsFromAPI(apiBaseURL string) (*domain.LLMModelsListResponse, error) {
	return &domain.LLMModelsListResponse{
		Count:  len(defaultModels),
		Models: defaultModels,
	}, nil
}

// SetupAgent writes openclaw.json from the setup request and restarts the gateway.
func (s *Service) SetupAgent(data domain.SetupRequest) error {
	slog.Debug("checking openclaw in PATH", "component", "openclaw")
	if _, err := exec.LookPath("openclaw"); err != nil {
		return fmt.Errorf("openclaw not found in PATH: %w", err)
	}
	slog.Debug("openclaw found", "component", "openclaw")

	llmAPIKey := data.LLMAPIKey
	llmBaseURL := data.LLMBaseURL
	llmModel := data.LLMModel
	if llmModel == "" {
		llmModel = s.config.LLMModel
	}
	channel := data.EffectiveChannel()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		slog.Debug("config does not exist, running onboardOpenclaw", "component", "openclaw")
		if err := s.onboardOpenclaw(); err != nil {
			return fmt.Errorf("onboard openclaw: %w", err)
		}
	}
	slog.Debug("loading config", "component", "openclaw", "path", configPath)
	var configData map[string]interface{}
	if data, err := os.ReadFile(configPath); err == nil {
		if err := json.Unmarshal(data, &configData); err != nil {
			return fmt.Errorf("parse openclaw config: %w", err)
		}
		slog.Debug("config loaded and parsed", "component", "openclaw")
	} else {
		configData = make(map[string]interface{})
		slog.Debug("no existing config, starting fresh", "component", "openclaw")
	}

	slog.Debug("listing models from API", "component", "openclaw", "baseURL", llmBaseURL)
	modelsResp, err := s.listModelsFromAPI(llmBaseURL)
	if err != nil {
		return fmt.Errorf("list llm models from api: %w", err)
	}
	slog.Debug("got models from API", "component", "openclaw", "count", len(modelsResp.Models))

	if len(modelsResp.Models) == 0 {
		return fmt.Errorf("no llm models found")
	}

	slog.Debug("resolving config model in list", "component", "openclaw", "model", llmModel)
	defaultModel, err := findModelByLLMModel(modelsResp.Models, llmModel)
	if err != nil {
		return err
	}
	slog.Debug("selected default model", "component", "openclaw", "key", defaultModel.Key, "name", defaultModel.Name)

	slog.Debug("building models.providers.autonomous", "component", "openclaw")
	modelsMap := ensureMap(configData, "models")
	modelsMap["mode"] = "merge"
	providersMap := ensureMap(modelsMap, "providers")
	modelsEntries := make([]any, 0, len(modelsResp.Models))
	for _, m := range modelsResp.Models {
		if s.config.LLMThinkingDisabled() {
			m.Reasoning = false
		}
		modelsEntries = append(modelsEntries, openclawModelToProviderEntry(m))
	}
	providersMap[customProviderName] = map[string]any{
		"baseUrl": withOpenAIV1(llmBaseURL),
		"api":     defaultModel.OpenClawAPIType(),
		"apiKey":  llmAPIKey,
		"models":  modelsEntries,
	}
	configData["models"] = modelsMap

	slog.Debug("building agents.defaults", "component", "openclaw")
	agentsMap := ensureMap(configData, "agents")
	defaultsMap := ensureMap(agentsMap, "defaults")
	workspace := filepath.Join(s.config.OpenclawConfigDir, "workspace")
	defaultsMap["workspace"] = workspace
	defaultsMap["elevatedDefault"] = "full"
	sandboxMap := ensureMap(defaultsMap, "sandbox")
	sandboxMap["mode"] = "off"
	defaultsMap["sandbox"] = sandboxMap
	compactionMap := ensureMap(defaultsMap, "compaction")
	compactionMap["mode"] = "safeguard"
	compactionMap["reserveTokensFloor"] = 80000
	defaultsMap["compaction"] = compactionMap
	defaultsMap["bootstrapMaxChars"] = 5000
	defaultsMap["bootstrapTotalMaxChars"] = 30000
	agentModelsMap := ensureMap(defaultsMap, "models")
	for _, m := range modelsResp.Models {
		// Use prefixed key "{provider}/{key}" so the on-disk shape matches what
		// the periodic model sync (mergeAgentModels) writes — avoids a one-time
		// migrate+restart on the first sync tick after setup.
		agentModelsMap[agentModelKey(m)] = map[string]any{
			"params": map[string]any{
				"cacheRetention": "short",
			},
		}
	}
	defaultsMap["model"] = map[string]any{
		"primary": fmt.Sprintf("%s/%s", customProviderName, defaultModel.Name),
	}
	defaultsMap["models"] = agentModelsMap
	agentsMap["defaults"] = defaultsMap
	configData["agents"] = agentsMap

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch channel {
	case "slack":
		slog.Debug("setting channels.slack (socket mode)", "component", "openclaw")
		slackMap := ensureMap(channelsMap, "slack")
		slackMap["enabled"] = true
		slackMap["mode"] = "socket"
		slackMap["botToken"] = data.SlackBotToken
		slackMap["appToken"] = data.SlackAppToken
		if data.SlackUserID != "" {
			slackMap["dmPolicy"] = "allowlist"
			slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], data.SlackUserID)
		} else {
			slackMap["dmPolicy"] = "open"
			slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], "*")
		}
		channelsMap["slack"] = slackMap
		if telegramMap, ok := channelsMap["telegram"].(map[string]any); ok {
			telegramMap["enabled"] = false
		}
		slackEntryMap := ensureMap(entriesMap, "slack")
		slackEntryMap["enabled"] = true
	case "discord":
		slog.Debug("setting channels.discord", "component", "openclaw")
		discordMap := ensureMap(channelsMap, "discord")
		discordMap["enabled"] = true
		discordMap["dmPolicy"] = "allowlist"
		discordMap["token"] = data.DiscordBotToken
		discordMap["allowFrom"] = mergeStringList(discordMap["allowFrom"], data.DiscordUserID)
		if data.DiscordGuildID != "" {
			discordMap["groupPolicy"] = "allowlist"
			discordMap["guilds"] = map[string]any{
				data.DiscordGuildID: map[string]any{
					"requireMention": false,
					"users": []string{
						data.DiscordUserID,
					},
				},
			}
		}
		channelsMap["discord"] = discordMap
		discordEntryMap := ensureMap(entriesMap, "discord")
		discordEntryMap["enabled"] = true
	default:
		slog.Debug("setting channels.telegram", "component", "openclaw")
		telegramMap := ensureMap(channelsMap, "telegram")
		telegramMap["enabled"] = true
		telegramMap["botToken"] = data.TelegramBotToken
		if data.TelegramUserID != "" {
			telegramMap["dmPolicy"] = "allowlist"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], data.TelegramUserID)
		} else {
			telegramMap["dmPolicy"] = "open"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], "*")
		}
		channelsMap["telegram"] = telegramMap
		telegramEntryMap := ensureMap(entriesMap, "telegram")
		telegramEntryMap["enabled"] = true
	}
	configData["channels"] = channelsMap

	slog.Debug("ensuring gateway defaults", "component", "openclaw")
	gatewayMap := ensureMap(configData, "gateway")
	setDefaultValue(gatewayMap, "mode", defaultGatewayMode)
	setDefaultValue(gatewayMap, "bind", defaultGatewayBind)
	setDefaultValue(gatewayMap, "port", defaultGatewayPort)
	gatewayAuthMap := ensureMap(gatewayMap, "auth")
	setDefaultValue(gatewayAuthMap, "mode", "token")
	if existingToken := strings.TrimSpace(getStringValue(gatewayAuthMap, "token")); existingToken == "" {
		token, err := generateGatewayToken()
		if err != nil {
			return fmt.Errorf("generate gateway token: %w", err)
		}
		gatewayAuthMap["token"] = token
	}
	gatewayMap["auth"] = gatewayAuthMap
	configData["gateway"] = gatewayMap

	slog.Debug("ensuring full-access tools defaults", "component", "openclaw")
	toolsMap := ensureMap(configData, "tools")
	toolsMap["profile"] = "full"
	execMap := ensureMap(toolsMap, "exec")
	execMap["host"] = "gateway"
	execMap["security"] = "full"
	execMap["ask"] = "off"
	toolsMap["exec"] = execMap
	elevatedMap := ensureMap(toolsMap, "elevated")
	elevatedMap["enabled"] = true
	elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
	elevatedAllowFrom[channel] = []any{"*"}
	elevatedMap["allowFrom"] = elevatedAllowFrom
	toolsMap["elevated"] = elevatedMap
	configData["tools"] = toolsMap

	slog.Debug("ensuring messages defaults", "component", "openclaw")
	messagesMap := ensureMap(configData, "messages")
	messagesMap["responsePrefix"] = "auto"
	messagesMap["ackReactionScope"] = "all"
	messagesMap["removeAckAfterReply"] = true
	configData["messages"] = messagesMap

	slog.Debug("ensuring logging defaults", "component", "openclaw")
	loggingMap := ensureMap(configData, "logging")
	loggingMap["consoleStyle"] = "pretty"
	loggingMap["file"] = "/var/log/openclaw/lamp.log"
	loggingMap["level"] = "debug"
	loggingMap["consoleLevel"] = "debug"
	configData["logging"] = loggingMap

	slog.Debug("ensuring commands defaults", "component", "openclaw")
	commandsMap := ensureMap(configData, "commands")
	commandsMap["native"] = true
	commandsMap["nativeSkills"] = true
	commandsMap["text"] = true
	commandsMap["bash"] = true
	commandsMap["bashForegroundMs"] = 2000
	commandsMap["config"] = true
	commandsMap["debug"] = true
	commandsMap["restart"] = true
	commandsMap["useAccessGroups"] = false
	commandsMap["ownerAllowFrom"] = []any{"*"}
	configData["commands"] = commandsMap

	slog.Debug("ensuring skills defaults", "component", "openclaw")
	skillsMap := ensureMap(configData, "skills")
	loadMap := ensureMap(skillsMap, "load")
	skillsDir := filepath.Join(workspace, "skills")
	loadMap["extraDirs"] = []any{skillsDir}
	loadMap["watch"] = true
	skillsMap["load"] = loadMap
	configData["skills"] = skillsMap

	slog.Debug("marshalling and writing openclaw.json", "component", "openclaw")
	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	if err := os.MkdirAll(s.config.OpenclawConfigDir, 0755); err != nil {
		return fmt.Errorf("create openclaw config dir: %w", err)
	}
	// Serialise flag+file write under primarySyncMu so this cannot interleave
	// with the watcher (syncPrimaryFromFile) or other openclaw.json writers.
	expectedPrimary := customProviderName + "/" + defaultModel.Key
	s.primarySyncMu.Lock()
	setLampWriteFlag(s.config.OpenclawConfigDir, expectedPrimary)
	writeErr := os.WriteFile(configPath, written, 0600)
	s.primarySyncMu.Unlock()
	if writeErr != nil {
		return fmt.Errorf("write openclaw config: %w", writeErr)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath)

	slog.Debug("restarting openclaw gateway", "component", "openclaw")
	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("gateway restart completed", "component", "openclaw")
	return nil
}

// AddChannel adds a messaging channel to openclaw.json (multi-channel) and restarts the gateway.
//
// For non-whatsapp channels this is a pure on-disk overlay + gateway restart.
// For whatsapp the canonical block is bootstrapped via the openclaw CLI
// (`channels add --channel whatsapp`) so defaults from upstream (accounts.default,
// mediaMaxMb) ride through unchanged; the plugin is also enabled/installed.
// ctx flows through to all subprocess calls so the MQTT 10-minute cap bounds
// the whole flow.
func (s *Service) AddChannel(ctx context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()

	// Hold primarySyncMu for the full read-modify-write cycle so this cannot
	// interleave with SyncModelsFromAPI or RefreshModelsConfig writing a newer
	// version of openclaw.json between our ReadFile and our WriteFile.
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	var configData map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		if err := json.Unmarshal(raw, &configData); err != nil {
			return fmt.Errorf("parse openclaw config: %w", err)
		}
	} else {
		return fmt.Errorf("read openclaw config: %w (device must be set up first)", err)
	}

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch channel {
	case domain.ChannelSlack:
		slackMap := ensureMap(channelsMap, domain.ChannelSlack)
		slackMap["enabled"] = true
		slackMap["botToken"] = data.SlackBotToken
		if data.SlackUserID != "" {
			slackMap["dmPolicy"] = "allowlist"
			slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], data.SlackUserID)
		} else {
			slackMap["dmPolicy"] = "open"
			slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], "*")
		}
		// Mode-specific block. Default (empty / "socket") preserves pre-existing
		// behaviour so installs without slack_mode set stay byte-identical.
		// HTTP mode is the message-loss-tolerant path: a public proxy forwards
		// Slack events over MQTT to this device's slack_event handler, which
		// POSTs them to the local gateway's webhookPath — so the gateway needs
		// the signing secret (to re-verify) and no Slack WebSocket / appToken.
		if data.EffectiveSlackMode() == "http" {
			slackMap["mode"] = "http"
			slackMap["signingSecret"] = data.SlackSigningSecret
			webhookPath := data.SlackWebhookPath
			if webhookPath == "" {
				webhookPath = "/slack/events"
			}
			slackMap["webhookPath"] = webhookPath
			delete(slackMap, "appToken")
		} else {
			slackMap["mode"] = "socket"
			slackMap["appToken"] = data.SlackAppToken
			delete(slackMap, "signingSecret")
			delete(slackMap, "webhookPath")
		}
		channelsMap[domain.ChannelSlack] = slackMap
		slackEntryMap := ensureMap(entriesMap, domain.ChannelSlack)
		slackEntryMap["enabled"] = true
	case domain.ChannelDiscord:
		discordMap := ensureMap(channelsMap, domain.ChannelDiscord)
		discordMap["enabled"] = true
		discordMap["dmPolicy"] = "allowlist"
		discordMap["token"] = data.DiscordBotToken
		discordMap["allowFrom"] = mergeStringList(discordMap["allowFrom"], data.DiscordUserID)
		if data.DiscordGuildID != "" {
			discordMap["groupPolicy"] = "allowlist"
			discordMap["guilds"] = map[string]any{
				data.DiscordGuildID: map[string]any{
					"requireMention": false,
					"users": []string{
						data.DiscordUserID,
					},
				},
			}
		}
		channelsMap[domain.ChannelDiscord] = discordMap
		discordEntryMap := ensureMap(entriesMap, domain.ChannelDiscord)
		discordEntryMap["enabled"] = true
	case domain.ChannelWhatsapp:
		// Bootstrap the canonical channels.whatsapp block via the CLI; it sets
		// defaults (accounts.default, mediaMaxMb, etc.) we'd otherwise have to
		// mirror by hand.
		if err := runOpenclawCLI(ctx, "channels", "add", "--channel", domain.ChannelWhatsapp); err != nil {
			return fmt.Errorf("openclaw channels add whatsapp: %w", err)
		}
		// channels add mutated openclaw.json on disk — reload before overlay.
		raw, err := os.ReadFile(configPath)
		if err != nil {
			return fmt.Errorf("re-read openclaw config after channels add: %w", err)
		}
		if err := json.Unmarshal(raw, &configData); err != nil {
			return fmt.Errorf("re-parse openclaw config after channels add: %w", err)
		}
		channelsMap = ensureMap(configData, "channels")
		pluginsMap = ensureMap(configData, "plugins")
		entriesMap = ensureMap(pluginsMap, "entries")

		whatsappMap := ensureMap(channelsMap, domain.ChannelWhatsapp)
		applyWhatsappChannelConfig(whatsappMap, data.WhatsappUserID)
		channelsMap[domain.ChannelWhatsapp] = whatsappMap
		// Try enable first (works on bundled releases). If that fails, install
		// + enable (externalized plugin model).
		if err := runOpenclawCLI(ctx, "plugins", "enable", domain.ChannelWhatsapp); err != nil {
			slog.Warn("plugins enable whatsapp failed, attempting install", "component", "openclaw", "error", err)
			if installErr := runOpenclawCLI(ctx, "plugins", "install", whatsappPluginPackage); installErr != nil {
				return fmt.Errorf("plugins install %s: %w", whatsappPluginPackage, installErr)
			}
			if err := runOpenclawCLI(ctx, "plugins", "enable", domain.ChannelWhatsapp); err != nil {
				return fmt.Errorf("plugins enable whatsapp after install: %w", err)
			}
		}
		whatsappEntryMap := ensureMap(entriesMap, domain.ChannelWhatsapp)
		whatsappEntryMap["enabled"] = true
	default:
		telegramMap := ensureMap(channelsMap, domain.ChannelTelegram)
		telegramMap["enabled"] = true
		telegramMap["botToken"] = data.TelegramBotToken
		if data.TelegramUserID != "" {
			telegramMap["dmPolicy"] = "allowlist"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], data.TelegramUserID)
		} else {
			telegramMap["dmPolicy"] = "open"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], "*")
		}
		channelsMap[domain.ChannelTelegram] = telegramMap
		telegramEntryMap := ensureMap(entriesMap, domain.ChannelTelegram)
		telegramEntryMap["enabled"] = true
	}
	configData["channels"] = channelsMap

	// Add elevated.allowFrom for the new channel
	if toolsMap, ok := configData["tools"].(map[string]any); ok {
		if elevatedMap, ok := toolsMap["elevated"].(map[string]any); ok {
			elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
			elevatedAllowFrom[channel] = []any{"*"}
			elevatedMap["allowFrom"] = elevatedAllowFrom
		}
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	// AddChannel does not change the primary model — write the existing primary
	// into the flag so the watcher correctly identifies this as a Lamp write.
	// primarySyncMu is already held for the full RMW cycle (acquired at entry).
	existingPrimary := extractPrimaryModel(configData)
	if existingPrimary != "" {
		setLampWriteFlag(s.config.OpenclawConfigDir, existingPrimary)
	}
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath, "channel", channel)

	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("gateway restarted", "component", "openclaw")
	return nil
}

// ResetAgent overwrites openclaw.json with a minimal default config and restarts the gateway.
func (s *Service) ResetAgent() error {
	slog.Debug("checking openclaw in PATH", "component", "openclaw")
	if _, err := exec.LookPath("openclaw"); err != nil {
		return fmt.Errorf("openclaw not found in PATH: %w", err)
	}
	slog.Debug("openclaw found", "component", "openclaw")
	if err := os.RemoveAll(s.config.OpenclawConfigDir); err != nil {
		return fmt.Errorf("remove openclaw config dir: %w", err)
	}
	if err := os.MkdirAll(s.config.OpenclawConfigDir, 0755); err != nil {
		return fmt.Errorf("recreate openclaw config dir: %w", err)
	}
	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	if err := s.onboardOpenclaw(); err != nil {
		return fmt.Errorf("onboard openclaw: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote default config", "component", "openclaw", "path", configPath)

	slog.Debug("restarting openclaw gateway", "component", "openclaw")
	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("reset completed", "component", "openclaw")
	return nil
}

// RefreshModelsConfig patches the models reasoning fields in openclaw.json
// based on current config and restarts the agent. Safe to call after UpdateConfig.
// Holds primarySyncMu for the entire read-modify-write cycle so it cannot
// interleave with other openclaw.json writers (watcher, model-sync, setup).
func (s *Service) RefreshModelsConfig() error {
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(data, &configData); err != nil {
		return fmt.Errorf("parse openclaw config: %w", err)
	}

	disableThinking := s.config.LLMThinkingDisabled()
	// Read LLMModel under config.mu so it cannot race with a concurrent
	// WithLockSave call. Lock order: primarySyncMu (held) → config.mu (acquired
	// here briefly) — consistent with syncPrimaryFromFile's order.
	currentModel := s.config.LLMModelKey()

	// Patch models.providers.autonomous.models[*].reasoning
	if modelsMap, ok := configData["models"].(map[string]any); ok {
		if providersMap, ok := modelsMap["providers"].(map[string]any); ok {
			if providerEntry, ok := providersMap[customProviderName].(map[string]any); ok {
				if modelsList, ok := providerEntry["models"].([]any); ok {
					for _, entry := range modelsList {
						if m, ok := entry.(map[string]any); ok {
							if disableThinking {
								m["reasoning"] = false
							} else {
								m["reasoning"] = true
							}
						}
					}
				}
			}
		}
	}

	// Conditionally sync agents.defaults.model.primary. Only overwrite it when
	// the current provider is autonomous — if the user switched OpenClaw to a
	// non-autonomous provider (e.g. openai/gpt-4) externally, we must not
	// silently reset it back to the Lamp-managed model.
	currentPrimary := extractPrimaryModel(configData)
	prov, _, _ := splitProviderModel(currentPrimary)
	var flagPrimary string // value written into the Lamp-write flag
	if currentPrimary == "" || prov == customProviderName {
		// No primary set yet, or it belongs to us — safe to update.
		newPrimary := customProviderName + "/" + currentModel
		agents := ensureMap(configData, "agents")
		defaults := ensureMap(agents, "defaults")
		modelMap := ensureMap(defaults, "model")
		modelMap["primary"] = newPrimary
		defaults["model"] = modelMap
		agents["defaults"] = defaults
		configData["agents"] = agents
		flagPrimary = newPrimary
		slog.Info("refreshed models config in openclaw.json", "component", "openclaw", "disableThinking", disableThinking, "primary", newPrimary)
	} else {
		// Non-autonomous provider is active; preserve it and log state drift so
		// operators know why the Lamp-side model and OpenClaw diverge.
		flagPrimary = currentPrimary
		slog.Warn("[refresh] non-autonomous provider active, skipping primary patch (state drift)",
			"current", currentPrimary, "lamp_model", s.config.LLMModel)
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	// Write the flag BEFORE the file so the watcher can match by content and
	// correctly skip this Lamp-initiated write regardless of the provider.
	setLampWriteFlag(s.config.OpenclawConfigDir, flagPrimary)
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	slog.Debug("wrote openclaw.json after models config refresh", "component", "openclaw", "disableThinking", disableThinking)

	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("restart completed after models config refresh", "component", "openclaw")
	return nil
}

// RestartAgent restarts the openclaw gateway only.
func (s *Service) RestartAgent() error {
	slog.Debug("restarting openclaw gateway", "component", "openclaw")
	if err := restartOpenclawGateway(); err != nil {
		return err
	}
	slog.Info("restart completed", "component", "openclaw")
	return nil
}

// withOpenAIV1 appends /v1 to autonomous API base URLs that are missing it.
// Only applies to autonomous.ai URLs ending with /ai (e.g. …/api/v1/ai).
// External providers are left untouched.
func withOpenAIV1(base string) string {
	base = strings.TrimSuffix(strings.TrimSpace(base), "/")
	if strings.Contains(base, "campaign-api.autonomous.ai") && strings.HasSuffix(base, "/ai") {
		return base + "/v1"
	}
	return base
}

func findModelByLLMModel(models []domain.LLMModel, llmModel string) (domain.LLMModel, error) {
	for _, m := range models {
		if m.Key == llmModel || strings.TrimPrefix(m.Key, fmt.Sprintf("%s/", customProviderName)) == llmModel || m.Name == llmModel {
			return m, nil
		}
	}
	return domain.LLMModel{}, fmt.Errorf("no model matching llm_model %q in openclaw models list", llmModel)
}

func openclawModelToProviderEntry(m domain.LLMModel) map[string]interface{} {
	contextWindow := 200000
	if m.ContextWindow != nil {
		contextWindow = *m.ContextWindow
	}
	maxTokens := 8192
	if m.MaxTokens != nil {
		maxTokens = *m.MaxTokens
	}
	return map[string]interface{}{
		"id":        m.Key,
		"name":      m.Name,
		"reasoning": m.Reasoning,
		"input":     m.Input,
		"cost": map[string]interface{}{
			"input":      0,
			"output":     0,
			"cacheRead":  0,
			"cacheWrite": 0,
		},
		"contextWindow": contextWindow,
		"maxTokens":     maxTokens,
	}
}
