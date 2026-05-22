package config

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"go-lamp.autonomous.ai/lib/mqtt"
)

const configPath = "config/config.json"

// LumiVersion is injected at build time via ldflags.
// Example:
//
//	-X go-lamp.autonomous.ai/server/config.LumiVersion=v1.2.3
var LumiVersion = "dev"

type Config struct {
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

	LLMAPIKey  string `json:"llm_api_key" yaml:"llmAPIKey" validate:"required"`
	LLMModel   string `json:"llm_model" yaml:"llmModel" validate:"required"`
	LLMBaseURL string `json:"llm_base_url" yaml:"llmBaseURL" validate:"required"`
	// STTBaseURL / TTSBaseURL override LLMBaseURL when STT or TTS lives on
	// a different host than the LLM. Empty = reuse LLMBaseURL.
	STTBaseURL string `json:"stt_base_url" yaml:"sttBaseURL"`
	TTSBaseURL string `json:"tts_base_url" yaml:"ttsBaseURL"`

	OTAMetadataURL  string `json:"ota_metadata_url" yaml:"otaMetadataURL"`
	OTAPollInterval string `json:"ota_poll_interval" yaml:"otaPollInterval"`

	DeepgramAPIKey string `json:"deepgram_api_key" yaml:"deepgramAPIKey"`
	// STTAPIKey is the API key for the AutonomousSTT (LLM-as-STT) backend
	// used when DeepgramAPIKey is empty. Empty falls back to LLMAPIKey so
	// existing one-key configs keep working; fill this when the STT account
	// is separate from the LLM account.
	STTAPIKey       string `json:"stt_api_key" yaml:"sttAPIKey"`
	// TTSAPIKey is the API key for the TTS provider (OpenAI, ElevenLabs, …).
	// Empty falls back to LLMAPIKey so existing one-key configs keep working;
	// fill this when the TTS account is separate from the LLM account.
	TTSAPIKey       string `json:"tts_api_key" yaml:"ttsAPIKey"`
	TTSProvider     string `json:"tts_provider" yaml:"ttsProvider"`
	TTSVoice        string `json:"tts_voice" yaml:"ttsVoice"`
	TTSInstructions string `json:"tts_instructions" yaml:"ttsInstructions"`

	// AgentRuntime selects which agentic backend to use: "openclaw" (default), "picoclaw", "claudecode", etc.
	AgentRuntime string `json:"agent_runtime" yaml:"agentRuntime"`

	OpenclawConfigDir string `json:"openclaw_config_dir" yaml:"openclawConfigDir"`

	NetworkSSID     string `json:"network_ssid" yaml:"networkSSID" validate:"required"`
	NetworkPassword string `json:"network_password" yaml:"networkPassword" validate:"required"`

	SetUpCompleted bool `json:"set_up_completed" yaml:"setUpCompleted"`

	// DeviceID is saved at setup, used for backend status reporting
	DeviceID string `json:"device_id" yaml:"deviceID"`

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

	// STTModel selects the speech-to-text model for lelamp.
	// Empty string means use lelamp's default (flux-general-en).
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

		OTAMetadataURL:  "",
		OTAPollInterval: "1h",

		OpenclawConfigDir: "/root/.openclaw",

		NetworkSSID:     "",
		NetworkPassword: "",
		SetUpCompleted:  false,
		DeviceID:        "",

		MQTTEndpoint: "",
		MQTTUsername: "",
		MQTTPassword: "",
		MQTTPort:     0,

		notify: make(chan bool, 1),
	}
}

func ProvideConfig() *Config {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		c := Default()
		if err := c.Save(); err != nil {
			slog.Error("save config failed", "component", "config", "error", err)
		}
		c.notify = make(chan bool, 1)
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

// Save writes the config to the config file.
func (c Config) Save() error {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}

	dir := filepath.Dir(configPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	if err := os.WriteFile(configPath, data, 0600); err != nil {
		return fmt.Errorf("write config %s: %w", configPath, err)
	}
	if c.notify != nil {
		select {
		case c.notify <- true:
		default:
		}
	}
	return nil
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
