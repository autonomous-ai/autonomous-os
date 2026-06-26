package config

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"go.autonomous.ai/os/lib/mqtt"
)

// bootstrapConfigPath is the OTA worker's config file. The device-wide OTA
// metadata URL is seeded there at provisioning (single source of truth);
// os-server's OTA-derived features (skill watcher, onboarding skills/hooks,
// OTA poller) read it from the same file rather than duplicating it here.
const bootstrapConfigPath = "/root/config/bootstrap.json"

// otaMetadataURLFromBootstrap returns metadata_url from bootstrap.json, or ""
// when the file is missing or invalid (e.g. device not yet provisioned).
func otaMetadataURLFromBootstrap() string {
	data, err := os.ReadFile(bootstrapConfigPath)
	if err != nil {
		return ""
	}
	var bc struct {
		MetadataURL string `json:"metadata_url"`
	}
	if err := json.Unmarshal(data, &bc); err != nil {
		return ""
	}
	return strings.TrimSpace(bc.MetadataURL)
}

const configPath = "config/config.json"

// OSVersion is injected at build time via ldflags.
// Example:
//
//	-X go.autonomous.ai/os/server/config.OSVersion=v1.2.3
var OSVersion = "dev"

type Config struct {
	// mu serialises LLMModel mutations and config.Save() so the primary-model
	// watcher goroutine (syncPrimaryFromFile) cannot race with HTTP handlers
	// (device.UpdateConfig) that set LLMModel concurrently.
	mu sync.Mutex

	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	// Channel type: "telegram" or "slack" (empty defaults to telegram for backward compat)
	Channel string `json:"channel" yaml:"channel"`

	TelegramBotToken string `json:"telegram_bot_token" yaml:"telegramBotToken"`
	TelegramUserID   string `json:"telegram_user_id" yaml:"telegramUserID"`

	SlackBotToken string `json:"slack_bot_token" yaml:"slackBotToken"`
	SlackAppToken string `json:"slack_app_token" yaml:"slackAppToken"`
	SlackUserID   string `json:"slack_user_id" yaml:"slackUserID"`

	DiscordBotToken string `json:"discord_bot_token" yaml:"discordBotToken"`
	DiscordGuildID  string `json:"discord_guild_id" yaml:"discordGuildID"`
	DiscordUserID   string `json:"discord_user_id" yaml:"discordUserID"`

	// WhatsappUserID is the E.164 phone number permitted to DM the device's
	// WhatsApp account. The Baileys session itself lives on disk at
	// <openclaw_config_dir>/credentials/whatsapp/<account>/creds.json — we never
	// persist its tokens here. Empty when no WhatsApp channel is configured.
	WhatsappUserID string `json:"whatsapp_user_id" yaml:"whatsappUserID"`

	// ChannelsAppliedRuntime is the agent runtime ChannelReconcile last applied the
	// configured channels for. When it differs from AgentRuntime on boot, the
	// reconcile re-applies the channels to the new runtime (and updates this).
	// Empty until the first reconcile records a baseline.
	ChannelsAppliedRuntime string `json:"channels_applied_runtime,omitempty" yaml:"channelsAppliedRuntime"`
	// ChannelsUnsupported lists channels configured here that the active runtime
	// cannot run, set by ChannelReconcile and surfaced on the MQTT info uplink.
	ChannelsUnsupported []string `json:"channels_unsupported,omitempty" yaml:"channelsUnsupported"`

	// MCPAppliedRuntime is the agent runtime MCPReconcile last cloned the configured
	// MCP connectors for. When it differs from AgentRuntime on boot, the reconcile
	// reads the previous runtime's MCP servers from its on-disk config and re-pushes
	// them into the new runtime (and updates this). Empty until the first reconcile
	// records a baseline. Mirrors ChannelsAppliedRuntime.
	MCPAppliedRuntime string `json:"mcp_applied_runtime,omitempty" yaml:"mcpAppliedRuntime"`

	LLMAPIKey  string `json:"llm_api_key" yaml:"llmAPIKey" validate:"required"`
	LLMModel   string `json:"llm_model" yaml:"llmModel" validate:"required"`
	LLMBaseURL string `json:"llm_base_url" yaml:"llmBaseURL" validate:"required"`

	// DefaultModelVersion is the upstream model-catalog version last applied by
	// the set-default-model flow (setup + periodic sync). The sync only pushes
	// default_model / default_image_model into openclaw.json when the freshly
	// fetched version is greater than this, so a steady catalog never triggers
	// redundant gateway restarts. 0 before the first versioned catalog applies.
	DefaultModelVersion int `json:"default_model_version" yaml:"defaultModelVersion"`
	// STTBaseURL / TTSBaseURL override LLMBaseURL when STT or TTS lives on
	// a different host than the LLM. Empty = reuse LLMBaseURL.
	STTBaseURL string `json:"stt_base_url" yaml:"sttBaseURL"`
	TTSBaseURL string `json:"tts_base_url" yaml:"ttsBaseURL"`

	// OTAMetadataURL is not persisted in config.json — it is sourced at load from
	// /root/config/bootstrap.json (single source of truth, see ProvideConfig).
	// In-memory only; consumers (skill watcher, onboarding) read it here.
	OTAMetadataURL string `json:"-" yaml:"-"`

	DeepgramAPIKey string `json:"deepgram_api_key" yaml:"deepgramAPIKey"`
	// STTAPIKey is the API key for the AutonomousSTT (LLM-as-STT) backend
	// used when DeepgramAPIKey is empty. Empty falls back to LLMAPIKey so
	// existing one-key configs keep working; fill this when the STT account
	// is separate from the LLM account.
	STTAPIKey string `json:"stt_api_key" yaml:"sttAPIKey"`
	// TTSAPIKey is the API key for the TTS provider (OpenAI, ElevenLabs, …).
	// Empty falls back to LLMAPIKey so existing one-key configs keep working;
	// fill this when the TTS account is separate from the LLM account.
	TTSAPIKey       string `json:"tts_api_key" yaml:"ttsAPIKey"`
	TTSProvider     string `json:"tts_provider" yaml:"ttsProvider"`
	TTSVoice        string `json:"tts_voice" yaml:"ttsVoice"`
	TTSInstructions string `json:"tts_instructions" yaml:"ttsInstructions"`

	// AgentRuntime selects which agentic backend to use: "openclaw" (default), "hermes", "picoclaw", "claudecode", etc.
	AgentRuntime string `json:"agent_runtime" yaml:"agentRuntime"`

	// Realtime configures the realtime voice agent (audio-native brain — Gemini
	// Live / OpenAI Realtime). Sibling selector to AgentRuntime: AgentRuntime picks
	// the turn-based text brain, Realtime picks the live-audio brain. Grouped under
	// one "realtime" JSON key (the first nested sub-object in this config) because
	// it carries shared + per-provider knobs. Pointer only so the key omits cleanly
	// from config.json when unconfigured — a nil block is NOT "off": the accessors
	// default to HAL's own defaults (enabled + provider gemini), so realtime runs
	// out of the box exactly as before. See RealtimeConfig and the Realtime*
	// accessors below.
	Realtime *RealtimeConfig `json:"realtime,omitempty" yaml:"realtime"`

	OpenclawConfigDir string `json:"openclaw_config_dir" yaml:"openclawConfigDir"`

	NetworkSSID     string `json:"network_ssid" yaml:"networkSSID" validate:"required"`
	NetworkPassword string `json:"network_password" yaml:"networkPassword" validate:"required"`

	SetUpCompleted bool `json:"set_up_completed" yaml:"setUpCompleted"`

	// DeviceID is saved at setup, used for backend status reporting
	DeviceID string `json:"device_id" yaml:"deviceID"`

	// DeviceType is the device class/profile id — the folder name under devices/
	// (e.g. "lamp", "intern-v2", "unitree-go2w"). Selects which DEVICE.md/SOUL.md the
	// runtime loads. Empty resolves to "" — no "lamp" fallback (see DeviceTypeOrDefault;
	// the Serve startup guard fail-louds). HAL reads the same key from config.json via
	// _os_cfg_get("device_type").
	DeviceType string `json:"device_type,omitempty" yaml:"deviceType"`

	// MQTT (optional): empty broker URL means MQTT disabled
	MQTTEndpoint string `json:"mqtt_endpoint" yaml:"mqttEndpoint"`
	MQTTUsername string `json:"mqtt_username" yaml:"mqttUsername"`
	MQTTPassword string `json:"mqtt_password" yaml:"mqttPassword"`
	MQTTPort     int    `json:"mqtt_port" yaml:"mqttPort"`
	FAChannel    string `json:"fa_channel" yaml:"faChannel"`
	FDChannel    string `json:"fd_channel" yaml:"fdChannel"`

	// LocalIntent enables local keyword matching for common voice commands (default true).
	// When false, all voice commands go through the agent (OpenClaw).
	LocalIntent *bool `json:"local_intent,omitempty" yaml:"localIntent"`

	// LLMDisableThinking disables extended thinking/reasoning for all LLM models (default false).
	// Enable this to reduce latency on fast models like Haiku that don't benefit from thinking.
	LLMDisableThinking *bool `json:"llm_disable_thinking,omitempty" yaml:"llmDisableThinking"`

	// STTModel selects the speech-to-text model for hal.
	// Empty string means use hal's default (flux-general-en).
	// Example: "nova-3" to enable Deepgram Nova 3 with language support.
	STTModel string `json:"stt_model,omitempty" yaml:"sttModel"`

	// STTLanguage sets the BCP-47 language code for STT (e.g. "vi", "en").
	// Only used when STTModel is non-empty. Empty means auto-detect.
	STTLanguage string `json:"stt_language,omitempty" yaml:"sttLanguage"`

	// GuardMode enables guard/security mode (default false).
	// When enabled, stranger/motion sensing events are broadcast to all chat sessions
	// instead of being spoken via TTS.
	GuardMode *bool `json:"guard_mode,omitempty" yaml:"guardMode"`

	// GuardInstruction is a custom instruction the owner provides when enabling guard mode.
	// Injected into sensing events so the agent follows it (e.g. "play scary sound when stranger detected").
	GuardInstruction string `json:"guard_instruction,omitempty" yaml:"guardInstruction"`

	// AdminPasswordHash is the bcrypt hash of the admin login password set during
	// device setup. POST /api/login validates against this. Empty before setup
	// completes; once set, /login becomes the canonical browser admin entry.
	AdminPasswordHash string `json:"admin_password_hash,omitempty" yaml:"adminPasswordHash"`

	// SessionSecret is a random 32-byte key (base64) used to sign HMAC session
	// tokens. Generated on first save when empty so an upgrade picks one up
	// automatically; rotating it invalidates all outstanding sessions.
	SessionSecret string `json:"session_secret,omitempty" yaml:"sessionSecret"`

	notify chan bool
}

// Load reads config from configPath. Returns error if file is missing or invalid.
func Load() (Config, error) {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		return Default(), fmt.Errorf("config file not found: %s", configPath)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		return Default(), fmt.Errorf("read config %s: %w", configPath, err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return Default(), fmt.Errorf("parse config %s: %w", configPath, err)
	}
	cfg.notify = make(chan bool, 1)
	return cfg, nil
}

func Default() Config {
	return Config{
		HttpPort: 5000,

		TelegramBotToken: "",

		LLMAPIKey:  "",
		LLMModel:   "claude-opus-4-6",
		LLMBaseURL: "",

		OTAMetadataURL: "",

		OpenclawConfigDir: "/root/.openclaw",

		NetworkSSID:     "",
		NetworkPassword: "",
		SetUpCompleted:  false,
		DeviceID:        "",

		MQTTEndpoint: "",
		MQTTUsername: "",
		MQTTPassword: "",
		MQTTPort:     0,

		// Seed the realtime block so a fresh config.json always carries an editable
		// realtime config (HAL reads it from there). See DefaultRealtimeConfig.
		Realtime: DefaultRealtimeConfig(),

		notify: make(chan bool, 1),
	}
}

// DeviceTypeOrDefault resolves the device class used to pick
// devices/<type>/{DEVICE,SOUL}.md. Order mirrors HAL's _resolve_device_type:
// the DEVICE_TYPE env (set at provisioning — an immutable hardware identity that
// must outrank anything the web UI writes) → config.json "device_type".
// Returns "" when unresolved — NO "lamp" fallback; the startup guard in Serve
// fail-louds rather than let a device masquerade as a lamp.
func (c *Config) DeviceTypeOrDefault() string {
	if t := os.Getenv("DEVICE_TYPE"); t != "" {
		return t
	}
	return c.DeviceType
}

func ProvideConfig() *Config {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		c := Default()
		if err := c.Save(); err != nil {
			slog.Error("save config failed", "component", "config", "error", err)
		}
		c.notify = make(chan bool, 1)
		c.OTAMetadataURL = otaMetadataURLFromBootstrap()
		return &c
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		panic(fmt.Errorf("read config %s: %w", configPath, err))
	}

	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		panic(fmt.Errorf("parse config %s: %w", configPath, err))
	}
	cfg.notify = make(chan bool, 1)

	// Migrate old openclaw config dir /root/openclaw → /root/.openclaw on startup.
	if cfg.OpenclawConfigDir == "/root/openclaw" {
		if err := migrateOpenclawDir("/root/openclaw", "/root/.openclaw"); err != nil {
			slog.Error("openclaw dir migration failed", "component", "config", "error", err)
		} else {
			cfg.OpenclawConfigDir = "/root/.openclaw"
			if err := cfg.Save(); err != nil {
				slog.Error("save config after migration failed", "component", "config", "error", err)
			}
		}
	}

	// Seed the realtime block with defaults if an already-provisioned config.json
	// predates it, so the file always carries an editable realtime config (HAL
	// reads it from there). Idempotent — only the first start after upgrade writes.
	if cfg.Realtime == nil {
		cfg.Realtime = DefaultRealtimeConfig()
		if err := cfg.Save(); err != nil {
			slog.Error("seed realtime config failed", "component", "config", "error", err)
		}
	}

	// OTA metadata URL lives in bootstrap.json (single source of truth); config.json
	// does not persist it (field is json:"-"). Empty when not yet provisioned.
	cfg.OTAMetadataURL = otaMetadataURLFromBootstrap()

	return &cfg
}

// ResetToDefault resets all config fields to default values (keeps notify channel) and saves.
// Used e.g. by the physical reset button (press-and-hold >= 10s).
func (c *Config) ResetToDefault() error {
	notify := c.notify
	*c = Default()
	c.notify = notify
	return c.Save()
}

// WithLockSave is the canonical way to mutate config fields and persist them.
// It acquires mu, runs fn (which may set any fields on c), marshals the result,
// and writes to disk — all under the same lock so two concurrent callers cannot
// produce a "newer marshal wins the race but older write lands last" stale
// snapshot on disk.
//
// The notify send happens after the lock is released to keep the critical
// section as short as possible.
func (c *Config) WithLockSave(fn func(*Config)) error {
	c.mu.Lock()
	fn(c)
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		c.mu.Unlock()
		return fmt.Errorf("marshal config: %w", err)
	}
	dir := filepath.Dir(configPath)
	if mkErr := os.MkdirAll(dir, 0755); mkErr != nil {
		c.mu.Unlock()
		return fmt.Errorf("create config dir: %w", mkErr)
	}
	writeErr := os.WriteFile(configPath, data, 0600)
	c.mu.Unlock() // release before notify so listeners are not blocked
	if writeErr != nil {
		return fmt.Errorf("write config %s: %w", configPath, writeErr)
	}
	if c.notify != nil {
		select {
		case c.notify <- true:
		default:
		}
	}
	return nil
}

// Save flushes the current config fields to disk under the config mutex.
// Prefer WithLockSave for any path that also mutates fields.
func (c *Config) Save() error {
	return c.WithLockSave(func(*Config) {})
}

// SetLLMModel atomically sets LLMModel and saves the config in a single lock
// cycle (no gap between the field write and the marshal). Intended for
// background goroutines (e.g. primary-model watcher) updating a single field.
func (c *Config) SetLLMModel(key string) error {
	return c.WithLockSave(func(c *Config) {
		c.LLMModel = key
	})
}

// LLMModelKey returns LLMModel under the config mutex. Use this in goroutines
// that read LLMModel concurrently with WithLockSave paths.
func (c *Config) LLMModelKey() string {
	c.mu.Lock()
	key := c.LLMModel
	c.mu.Unlock()
	return key
}

// GetTTSAPIKey returns the TTS provider API key, falling back to LLMAPIKey
// when TTSAPIKey is unset so configs that pre-date the split keep working.
func (c *Config) GetTTSAPIKey() string {
	if c.TTSAPIKey != "" {
		return c.TTSAPIKey
	}
	return c.LLMAPIKey
}

// GetSTTAPIKey returns the AutonomousSTT API key, falling back to LLMAPIKey
// when STTAPIKey is unset. Only used when DeepgramAPIKey is empty (Deepgram
// has its own key path).
func (c *Config) GetSTTAPIKey() string {
	if c.STTAPIKey != "" {
		return c.STTAPIKey
	}
	return c.LLMAPIKey
}

// GetSTTBaseURL returns the AutonomousSTT base URL, falling back to LLMBaseURL.
func (c *Config) GetSTTBaseURL() string {
	if c.STTBaseURL != "" {
		return c.STTBaseURL
	}
	return c.LLMBaseURL
}

// GetTTSBaseURL returns the TTS provider base URL, falling back to LLMBaseURL.
func (c *Config) GetTTSBaseURL() string {
	if c.TTSBaseURL != "" {
		return c.TTSBaseURL
	}
	return c.LLMBaseURL
}

// LocalIntentEnabled returns whether local intent matching is on (default true).
func (c *Config) LocalIntentEnabled() bool {
	if c.LocalIntent == nil {
		return true
	}
	return *c.LocalIntent
}

// LLMThinkingDisabled returns whether extended thinking is disabled (default false).
func (c *Config) LLMThinkingDisabled() bool {
	if c.LLMDisableThinking == nil {
		return false
	}
	return *c.LLMDisableThinking
}

// GuardModeEnabled returns whether guard mode is on (default false).
func (c *Config) GuardModeEnabled() bool {
	if c.GuardMode == nil {
		return false
	}
	return *c.GuardMode
}

func (c *Config) GetNotifyChannel() chan bool {
	return c.notify
}

func ProvideMQTTConfig(c *Config) mqtt.Config {
	return mqtt.Config{
		Endpoint: c.MQTTEndpoint,
		Username: c.MQTTUsername,
		Password: c.MQTTPassword,
		Port:     c.MQTTPort,
	}
}

// migrateOpenclawDir moves oldDir to newDir if oldDir exists and newDir does not.
func migrateOpenclawDir(oldDir, newDir string) error {
	if _, err := os.Stat(oldDir); os.IsNotExist(err) {
		return nil // nothing to migrate
	}
	if _, err := os.Stat(newDir); err == nil {
		return nil // destination already exists, skip
	}
	slog.Info("migrating openclaw config dir", "component", "config", "from", oldDir, "to", newDir)
	return os.Rename(oldDir, newDir)
}
