package domain

import (
	"encoding/json"
	"fmt"
	"time"

	"go-lamp.autonomous.ai/server/config"
)

type SetupRequest struct {
	// setup network
	SSID     string `json:"ssid" validate:"required"`
	Password string `json:"password" validate:"required"`

	// channel type: "telegram" (default), "slack" or "discord"
	Channel string `json:"channel"`

	// telegram channel (required when channel is telegram or empty)
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	// slack channel (required when channel is slack)
	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	// discord channel (required when channel is discord)
	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// setup custom provider for openclaw
	LLMBaseURL string `json:"llm_base_url" validate:"required"`
	LLMAPIKey  string `json:"llm_api_key" validate:"required"`
	LLMModel   string `json:"llm_model"`

	// voice pipeline (optional): Deepgram API key for STT
	DeepgramAPIKey string `json:"deepgram_api_key"`
	// STTAPIKey / TTSAPIKey override LLMAPIKey when those accounts are
	// separate. Empty = device falls back to LLMAPIKey. STTBaseURL /
	// TTSBaseURL likewise override LLMBaseURL.
	STTAPIKey      string `json:"stt_api_key"`
	TTSAPIKey      string `json:"tts_api_key"`
	STTBaseURL     string `json:"stt_base_url"`
	TTSBaseURL     string `json:"tts_base_url"`
	STTLanguage    string `json:"stt_language"`
	TTSProvider    string `json:"tts_provider"`
	TTSVoice       string `json:"tts_voice"`

	// optional
	DeviceID string `json:"device_id" validate:"required"`

	// AdminPassword is the plaintext password the operator picks at setup time.
	// Server bcrypts it into config.AdminPasswordHash and never persists the
	// plaintext. Used to gate browser admin access via POST /api/login + the
	// lumi_session cookie. Empty allowed (validated at handler level so
	// pre-login-UI clients keep working during the migration window).
	AdminPassword string `json:"admin_password"`

	// MQTT (optional): empty broker URL means MQTT disabled
	MQTTEndpoint string `json:"mqtt_endpoint"`
	MQTTUsername string `json:"mqtt_username"`
	MQTTPassword string `json:"mqtt_password"`
	MQTTPort     int    `json:"mqtt_port"`
	FAChannel    string `json:"fa_channel"`
	FDChannel    string `json:"fd_channel"`

	// LLMDisableThinking disables extended thinking/reasoning for all models (default false).
	LLMDisableThinking *bool `json:"llm_disable_thinking,omitempty"`
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram".
func (r *SetupRequest) EffectiveChannel() string {
	if r.Channel == "slack" {
		return "slack"
	}
	if r.Channel == "discord" {
		return "discord"
	}
	return "telegram"
}

// ValidateChannel checks that the required fields for the selected channel are present.
func (r *SetupRequest) ValidateChannel() error {
	switch r.EffectiveChannel() {
	case "slack":
		if r.SlackBotToken == "" {
			return fmt.Errorf("slack_bot_token is required for slack channel")
		}
		if r.SlackAppToken == "" {
			return fmt.Errorf("slack_app_token is required for slack channel")
		}
	case "discord":
		if r.DiscordBotToken == "" {
			return fmt.Errorf("discord_bot_token is required for discord channel")
		}
		if r.DiscordGuildID == "" {
			return fmt.Errorf("discord_guild_id is required for discord channel")
		}
		if r.DiscordUserID == "" {
			return fmt.Errorf("discord_user_id is required for discord channel")
		}
	default:
		if r.TelegramBotToken == "" {
			return fmt.Errorf("telegram_bot_token is required for telegram channel")
		}
		if r.TelegramUserID == "" {
			return fmt.Errorf("telegram_user_id is required for telegram channel")
		}
	}
	return nil
}

// AddChannelRequest is used to add a messaging channel after initial setup.
type AddChannelRequest struct {
	// channel type: "telegram", "slack" or "discord"
	Channel string `json:"channel" validate:"required"`

	// telegram
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	// slack
	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	// discord
	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram".
func (r *AddChannelRequest) EffectiveChannel() string {
	if r.Channel == "slack" {
		return "slack"
	}
	if r.Channel == "discord" {
		return "discord"
	}
	return "telegram"
}

// ValidateChannel checks that the required fields for the selected channel are present.
func (r *AddChannelRequest) ValidateChannel() error {
	switch r.EffectiveChannel() {
	case "slack":
		if r.SlackBotToken == "" {
			return fmt.Errorf("slack_bot_token is required for slack channel")
		}
		if r.SlackAppToken == "" {
			return fmt.Errorf("slack_app_token is required for slack channel")
		}
	case "discord":
		if r.DiscordBotToken == "" {
			return fmt.Errorf("discord_bot_token is required for discord channel")
		}
		if r.DiscordGuildID == "" {
			return fmt.Errorf("discord_guild_id is required for discord channel")
		}
		if r.DiscordUserID == "" {
			return fmt.Errorf("discord_user_id is required for discord channel")
		}
	default:
		if r.TelegramBotToken == "" {
			return fmt.Errorf("telegram_bot_token is required for telegram channel")
		}
		if r.TelegramUserID == "" {
			return fmt.Errorf("telegram_user_id is required for telegram channel")
		}
	}
	return nil
}

type SetupResponse struct {
	Success bool `json:"success"`
}

// Command types received from server via MQTT FAChannel.
// Matches spec: docs/mqtt_specs_autonomous.md
const (
	CommandInfo       = "info"
	CommandAddChannel = "add_channel"
	CommandOTA        = "ota"
	CommandData       = "data"
)

// KindTTSSet is the kind field for cmd:"data" tts.set downlinks from BFF.
const KindTTSSet = "tts.set"

// Message is the standard envelope for MQTT messages from the server (fa_channel).
// Server sends: {"cmd": "info"}, {"cmd": "add_channel", ...}, {"cmd": "data", "kind": "tts.set", ...}
type MQTTMessage struct {
	Cmd     string          `json:"cmd"`
	Kind    string          `json:"kind"`
	RawData json.RawMessage `json:"-"`
	raw     []byte
}

// UnmarshalJSON custom unmarshals to keep the full raw payload accessible to handlers.
func (m *MQTTMessage) UnmarshalJSON(data []byte) error {
	type alias struct {
		Cmd  string `json:"cmd"`
		Kind string `json:"kind"`
	}
	var a alias
	if err := json.Unmarshal(data, &a); err != nil {
		return err
	}
	m.Cmd = a.Cmd
	m.Kind = a.Kind
	m.raw = make([]byte, len(data))
	copy(m.raw, data)
	return nil
}

// Raw returns the full original JSON payload for handlers to parse additional fields.
func (m *MQTTMessage) Raw() []byte {
	return m.raw
}

type MQTTAddChannelRequest struct {
	Channel string                 `json:"channel" validate:"required"`
	Config  map[string]interface{} `json:"config"`
}

// MQTTAddChannelCommand is the fa_channel payload for cmd:"add_channel".
// Example: {"cmd":"add_channel","channel":"discord","config":{"bot_token":"...","guild_id":"..."}}
type MQTTAddChannelCommand struct {
	Channel string                 `json:"channel"`
	Config  map[string]interface{} `json:"config"`
}

func (r *MQTTAddChannelCommand) ToRequest() AddChannelRequest {
	var req AddChannelRequest
	req.Channel = r.Channel
	cfg := r.Config
	switch r.Channel {
	case "discord":
		req.DiscordBotToken, _ = cfg["bot_token"].(string)
		req.DiscordGuildID, _ = cfg["guild_id"].(string)
		req.DiscordUserID, _ = cfg["user_id"].(string)
	case "slack":
		req.SlackBotToken, _ = cfg["bot_token"].(string)
		req.SlackAppToken, _ = cfg["app_token"].(string)
		req.SlackUserID, _ = cfg["channel_id"].(string)
	default:
		req.TelegramBotToken, _ = cfg["bot_token"].(string)
		req.TelegramUserID, _ = cfg["chat_id"].(string)
	}
	return req
}

// MQTTAddChannelResponse extends MQTTInfoResponse with channel-specific fields for fd_channel.
type MQTTAddChannelResponse struct {
	MQTTInfoResponse
	Channel string `json:"channel"`
	Status  string `json:"status"`
	Error   string `json:"error,omitempty"`
}

type MQTTRemoveChannelRequest struct {
	Channel string `json:"channel" validate:"required"`
}

type MQTTRemoveChannelResponse struct {
	Success bool `json:"success"`
}

// DeviceMessage is the base response published to fd_channel.
// All messages MUST include these required fields per spec.
type MQTTInfoResponse struct {
	Device          string `json:"device"`
	Type            string `json:"type"`
	Version         string `json:"version"`
	ID              string `json:"id"`
	Mac             string `json:"mac"`
	Time            string `json:"time"`
	TTSProvider     string `json:"tts_provider,omitempty"`
	TTSVoice        string `json:"tts_voice,omitempty"`
	STTLanguage     string `json:"stt_language,omitempty"`
	LelampVersion   string `json:"lelamp_version,omitempty"`
	OpenClawVersion string `json:"openclaw_version,omitempty"`
	LocalIP         string `json:"local_ip,omitempty"`
}

// NewDeviceMessage creates a base message with required fields populated from config.
func NewMQTTInfoResponse(cfg *config.Config, msgType string, mac string) MQTTInfoResponse {
	return MQTTInfoResponse{
		Device:      "ai_lumi",
		Type:        msgType,
		Version:     config.LumiVersion,
		ID:          cfg.DeviceID,
		Mac:         mac,
		Time:        time.Now().UTC().Format(time.RFC3339Nano),
		TTSProvider: cfg.TTSProvider,
		TTSVoice:    cfg.TTSVoice,
		STTLanguage: cfg.STTLanguage,
	}
}

// MQTTTTSSetData is the nested data payload for cmd:"data", kind:"tts.set" downlinks.
// BFF sends: {"cmd":"data","kind":"tts.set","data":{"provider":"elevenlabs","voice":"Linh","language":"vi"}}
type MQTTTTSSetData struct {
	Provider string `json:"provider"`
	Voice    string `json:"voice"`
	Language string `json:"language"`
}

// MQTTTTSSetCommand wraps the full tts.set downlink envelope for unmarshalling.
type MQTTTTSSetCommand struct {
	Data MQTTTTSSetData `json:"data"`
}

// MQTTTTSSetAck is published to fd_channel after applying (or failing) a tts.set downlink.
// status: "starting" | "success" | "failure"
type MQTTTTSSetAck struct {
	MQTTInfoResponse
	Kind   string          `json:"kind"`
	Status string          `json:"status"`
	Error  string          `json:"error,omitempty"`
	Data   *MQTTTTSSetData `json:"data,omitempty"`
}

// ConfigPublicResponse is returned by GET /api/device/config. Raw secrets
// (API keys, channel tokens, MQTT/WiFi passwords) are replaced by boolean
// presence flags so the web UI can render "configured ✓" + a write-only
// SecretUpdateField. Non-secret fields (URLs, IDs, model name, language)
// are returned as-is because they're useful for the UI and not sensitive.
type ConfigPublicResponse struct {
	Channel            string `json:"channel"`
	TelegramUserID     string `json:"telegram_user_id"`
	SlackUserID        string `json:"slack_user_id"`
	DiscordGuildID     string `json:"discord_guild_id"`
	DiscordUserID      string `json:"discord_user_id"`
	LLMModel           string `json:"llm_model"`
	LLMBaseURL         string `json:"llm_base_url"`
	LLMDisableThinking bool   `json:"llm_disable_thinking"`
	STTBaseURL         string `json:"stt_base_url"`
	TTSBaseURL         string `json:"tts_base_url"`
	STTLanguage        string `json:"stt_language"`
	STTModel           string `json:"stt_model"`
	TTSProvider        string `json:"tts_provider"`
	TTSVoice           string `json:"tts_voice"`
	DeviceID           string `json:"device_id"`
	Mac                string `json:"mac"`
	NetworkSSID        string `json:"network_ssid"`
	MQTTEndpoint       string `json:"mqtt_endpoint"`
	MQTTUsername       string `json:"mqtt_username"`
	MQTTPort           int    `json:"mqtt_port"`
	FAChannel          string `json:"fa_channel"`
	FDChannel          string `json:"fd_channel"`

	// Presence booleans replace raw secret values. Frontend renders
	// "configured · update" affordance when true, empty input when false.
	HasTelegramBotToken bool `json:"has_telegram_bot_token"`
	HasSlackBotToken    bool `json:"has_slack_bot_token"`
	HasSlackAppToken    bool `json:"has_slack_app_token"`
	HasDiscordBotToken  bool `json:"has_discord_bot_token"`
	HasLLMAPIKey        bool `json:"has_llm_api_key"`
	HasDeepgramAPIKey   bool `json:"has_deepgram_api_key"`
	HasSTTAPIKey        bool `json:"has_stt_api_key"`
	HasTTSAPIKey        bool `json:"has_tts_api_key"`
	HasNetworkPassword  bool `json:"has_network_password"`
	HasMQTTPassword     bool `json:"has_mqtt_password"`
	HasAdminPassword    bool `json:"has_admin_password"`
}

// UpdateConfigRequest is used by PUT /api/device/config to update device settings.
// All fields are optional; only non-empty values are applied.
type UpdateConfigRequest struct {
	SSID     string `json:"ssid"`
	Password string `json:"password"`
	Channel  string `json:"channel"`

	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	LLMBaseURL         string `json:"llm_base_url"`
	LLMAPIKey          string `json:"llm_api_key"`
	LLMModel           string `json:"llm_model"`
	LLMDisableThinking *bool  `json:"llm_disable_thinking,omitempty"`

	DeepgramAPIKey string `json:"deepgram_api_key"`
	STTAPIKey      string `json:"stt_api_key"`
	TTSAPIKey      string `json:"tts_api_key"`
	STTBaseURL     string `json:"stt_base_url"`
	TTSBaseURL     string `json:"tts_base_url"`
	STTLanguage    string `json:"stt_language"`
	DeviceID       string `json:"device_id"`

	MQTTEndpoint string `json:"mqtt_endpoint"`
	MQTTUsername string `json:"mqtt_username"`
	MQTTPassword string `json:"mqtt_password"`
	MQTTPort     int    `json:"mqtt_port"`
	FAChannel    string `json:"fa_channel"`
	FDChannel    string `json:"fd_channel"`

	TTSProvider string `json:"tts_provider"`
	TTSVoice    string `json:"tts_voice"`

	// AdminPassword rotates the bcrypt hash when non-empty. Existing sessions
	// keep working (they ride config.SessionSecret, not the hash); to nuke
	// every outstanding session the operator must rotate SessionSecret too.
	AdminPassword string `json:"admin_password"`
}

// TTS provider constants.
const (
	TTSProviderOpenAI     = "openai"
	TTSProviderElevenLabs = "elevenlabs"
)

// TTSProviders is the list of supported TTS providers.
var TTSProviders = []string{TTSProviderOpenAI, TTSProviderElevenLabs}

// TTSVoicesByProvider maps provider name to its available voices.
var TTSVoicesByProvider = map[string][]string{
	TTSProviderOpenAI:     {"alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"},
	TTSProviderElevenLabs: {"Rachel", "Sarah", "Grace", "Freya", "Matilda", "Emily", "Alice", "Lily", "Charlotte", "Nicole", "Glinda", "Serena", "Jessie", "Brian", "Adam", "Daniel", "George", "James", "Liam", "Callum", "Harry", "Charlie", "Chris", "Sam"},
}

// TTSVoices is the default (OpenAI) voice list for backward compatibility.
var TTSVoices = TTSVoicesByProvider[TTSProviderOpenAI]

// DefaultTTSVoice is the default voice when none is configured.
const DefaultTTSVoice = "alloy"
