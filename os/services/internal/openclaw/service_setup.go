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

	"go.autonomous.ai/os/domain"
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

// SetupAgent writes openclaw.json from the setup request and restarts the gateway.
func (s *Service) SetupAgent(data domain.SetupRequest) error {
	slog.Debug("checking openclaw in PATH", "component", "openclaw")
	if _, err := exec.LookPath("openclaw"); err != nil {
		return fmt.Errorf("openclaw not found in PATH: %w", err)
	}
	slog.Debug("openclaw found", "component", "openclaw")

	llmAPIKey := data.LLMAPIKey
	llmBaseURL := data.LLMBaseURL
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

	// Fetch the live model catalog. On any failure fall back to the hardcoded
	// defaultModels so setup still completes when campaign-api is unreachable.
	slog.Debug("fetching models from API", "component", "openclaw")
	modelsResp, err := FetchModelsFromAPI()
	usedFallback := false
	if err != nil {
		slog.Warn("setup: model API fetch failed, using hardcoded fallback", "component", "openclaw", "err", err)
		modelsResp = &domain.LLMModelsListResponse{Count: len(defaultModels), Models: defaultModels}
		usedFallback = true
	}
	if len(modelsResp.Models) == 0 {
		return fmt.Errorf("no llm models found")
	}
	slog.Debug("got models", "component", "openclaw", "count", len(modelsResp.Models), "fallback", usedFallback)

	// Primary precedence: fetch OK → upstream default_model; fallback (API down)
	// → the user's selection already persisted in config.LLMModel (set by
	// device.Service.Setup before this call); else first in the catalog.
	wantKey := strings.TrimSpace(modelsResp.DefaultModel)
	if usedFallback {
		wantKey = strings.TrimSpace(s.config.LLMModel)
	}
	if wantKey == "" {
		wantKey = modelsResp.Models[0].Key
	}
	defaultModel, err := findModelByLLMModel(modelsResp.Models, wantKey)
	if err != nil {
		// Requested key not in the catalog — fall back to the first model so
		// setup never hard-fails on a stale/unknown selection.
		slog.Warn("setup: requested model not in catalog, using first", "component", "openclaw", "want", wantKey)
		defaultModel = modelsResp.Models[0]
	}
	slog.Debug("selected default model", "component", "openclaw", "key", defaultModel.Key)

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
		"baseUrl": llmBaseURL,
		"api":     resolveAutonomousAPI(modelsResp.API),
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
		// the periodic model sync (overwriteAgentAutonomousModels) writes —
		// avoids a one-time migrate+restart on the first sync tick after setup.
		agentModelsMap[agentModelKey(m)] = map[string]any{
			"params": map[string]any{
				"cacheRetention": "short",
			},
		}
	}
	defaultsMap["model"] = map[string]any{
		"primary": fmt.Sprintf("%s/%s", customProviderName, defaultModel.Key),
	}
	defaultsMap["models"] = agentModelsMap
	// Seed the default image/vision model from upstream when published. Gated by
	// presence only — fresh setup always seeds it on the autonomous provider.
	if img := strings.TrimSpace(modelsResp.DefaultImageModel); img != "" {
		defaultsMap["imageModel"] = map[string]any{
			"primary": fmt.Sprintf("%s/%s", customProviderName, img),
		}
	}
	agentsMap["defaults"] = defaultsMap
	configData["agents"] = agentsMap

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch channel {
	case "slack":
		slog.Debug("setting channels.slack", "component", "openclaw")
		// Slack is an externalized plugin (@openclaw/slack) — ensure it's
		// installed+enabled before writing config (self-heals a missing plugin).
		slackPluginCtx, slackCancel := context.WithTimeout(context.Background(), channelPluginInstallTimeout)
		slackPluginErr := ensureChannelPlugin(slackPluginCtx, domain.ChannelSlack, slackPluginPackage)
		slackCancel()
		if slackPluginErr != nil {
			return fmt.Errorf("ensure slack plugin: %w", slackPluginErr)
		}
		slackMap := ensureMap(channelsMap, "slack")
		// Initial setup always provisions Socket Mode; the HTTP-mode switch goes
		// through AddChannel after setup completes.
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken: data.SlackBotToken,
			AppToken: data.SlackAppToken,
			UserID:   data.SlackUserID,
			Runtime:  currentOpenclawRuntime(),
		})
		channelsMap["slack"] = slackMap
		if telegramMap, ok := channelsMap["telegram"].(map[string]any); ok {
			telegramMap["enabled"] = false
		}
		slackEntryMap := ensureMap(entriesMap, "slack")
		slackEntryMap["enabled"] = true
	case "discord":
		slog.Debug("setting channels.discord", "component", "openclaw")
		// Discord is an externalized plugin (@openclaw/discord) — ensure it's
		// installed+enabled before writing config. Enable is instant when the
		// plugin is already provisioned; this self-heals a missing plugin.
		pluginCtx, cancel := context.WithTimeout(context.Background(), channelPluginInstallTimeout)
		err := ensureChannelPlugin(pluginCtx, domain.ChannelDiscord, discordPluginPackage)
		cancel()
		if err != nil {
			return fmt.Errorf("ensure discord plugin: %w", err)
		}
		discordMap := ensureMap(channelsMap, "discord")
		applyDiscordChannelConfig(discordMap, data.DiscordBotToken, data.DiscordUserID, data.DiscordGuildID)
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
	loggingMap["file"] = "/var/log/openclaw/agent.log"
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
	setOSWriteFlag(s.config.OpenclawConfigDir, expectedPrimary)
	writeErr := os.WriteFile(configPath, written, 0600)
	s.primarySyncMu.Unlock()
	if writeErr != nil {
		return fmt.Errorf("write openclaw config: %w", writeErr)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath)

	// On a successful fetch, update config.LLMModel to the resolved upstream
	// default_model and record the catalog version. On fallback (API down) leave
	// config.LLMModel as-is — it already holds the user's selection set by
	// device.Service.Setup. The shared *config.Config is persisted by
	// device.Service.Setup's config.Save() after this returns.
	if !usedFallback {
		s.config.LLMModel = defaultModel.Key
		if modelsResp.Version > 0 {
			s.config.DefaultModelVersion = modelsResp.Version
		}
	}

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
		// Ensure the externalized @openclaw/slack plugin is installed+enabled
		// before writing config (self-heals a missing plugin).
		if err := ensureChannelPlugin(ctx, domain.ChannelSlack, slackPluginPackage); err != nil {
			return fmt.Errorf("ensure slack plugin: %w", err)
		}
		slackMap := ensureMap(channelsMap, domain.ChannelSlack)
		// HTTP mode is the message-loss-tolerant path: a public proxy forwards
		// Slack events over MQTT to this device's slack_event handler, which POSTs
		// them to the local gateway's webhookPath — so the gateway needs the signing
		// secret (to re-verify) and no Slack WebSocket / appToken. Socket mode opens
		// an outbound WSS to Slack and needs the appToken instead.
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken:      data.SlackBotToken,
			AppToken:      data.SlackAppToken,
			UserID:        data.SlackUserID,
			Mode:          data.SlackMode,
			SigningSecret: data.SlackSigningSecret,
			WebhookPath:   data.SlackWebhookPath,
			Runtime:       currentOpenclawRuntime(),
		})
		channelsMap[domain.ChannelSlack] = slackMap
		slackEntryMap := ensureMap(entriesMap, domain.ChannelSlack)
		slackEntryMap["enabled"] = true
	case domain.ChannelDiscord:
		// Ensure the externalized @openclaw/discord plugin is installed+enabled
		// before writing config (self-heals a missing plugin).
		if err := ensureChannelPlugin(ctx, domain.ChannelDiscord, discordPluginPackage); err != nil {
			return fmt.Errorf("ensure discord plugin: %w", err)
		}
		discordMap := ensureMap(channelsMap, domain.ChannelDiscord)
		applyDiscordChannelConfig(discordMap, data.DiscordBotToken, data.DiscordUserID, data.DiscordGuildID)
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
		// Ensure the externalized @openclaw/whatsapp plugin is installed+enabled.
		if err := ensureChannelPlugin(ctx, domain.ChannelWhatsapp, whatsappPluginPackage); err != nil {
			return err
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
	// into the flag so the watcher correctly identifies this as an os-server write.
	// primarySyncMu is already held for the full RMW cycle (acquired at entry).
	existingPrimary := extractPrimaryModel(configData)
	if existingPrimary != "" {
		setOSWriteFlag(s.config.OpenclawConfigDir, existingPrimary)
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

	// Patch models.providers.autonomous — baseUrl + per-model reasoning.
	currentBaseURL := s.config.LLMBaseURL
	if modelsMap, ok := configData["models"].(map[string]any); ok {
		if providersMap, ok := modelsMap["providers"].(map[string]any); ok {
			if providerEntry, ok := providersMap[customProviderName].(map[string]any); ok {
				if currentBaseURL != "" {
					providerEntry["baseUrl"] = currentBaseURL
				}
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
	// silently reset it back to the os-server-managed model.
	currentPrimary := extractPrimaryModel(configData)
	prov, _, _ := splitProviderModel(currentPrimary)
	var flagPrimary string // value written into the os-server-write flag
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
		// operators know why the os-server-side model and OpenClaw diverge.
		flagPrimary = currentPrimary
		slog.Warn("[refresh] non-autonomous provider active, skipping primary patch (state drift)",
			"current", currentPrimary, "os_model", s.config.LLMModel)
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	// Write the flag BEFORE the file so the watcher can match by content and
	// correctly skip this os-server-initiated write regardless of the provider.
	setOSWriteFlag(s.config.OpenclawConfigDir, flagPrimary)
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
