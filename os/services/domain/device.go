package domain

import (
	"encoding/json"
	"fmt"
	"time"

	"go.autonomous.ai/os/server/config"
)

// Channel type identifiers. WhatsApp is intentionally NOT a valid Setup
// channel — it can only be added post-setup via the MQTT add_channel command
// because pairing is interactive (QR streaming) and the captive-portal setup
// path can't carry a live event stream.
const (
	ChannelTelegram = "telegram"
	ChannelSlack    = "slack"
	ChannelDiscord  = "discord"
	ChannelWhatsapp = "whatsapp"
)

type SetupRequest struct {
	// setup network
	SSID     string `json:"ssid" validate:"required"`
	Password string `json:"password" validate:"required"`

	// channel type: "telegram" (default), "slack" or "discord".
	// WhatsApp is intentionally not accepted here — it must be added
	// post-setup via the MQTT add_channel command (streaming QR pairing).
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
	STTAPIKey   string `json:"stt_api_key"`
	TTSAPIKey   string `json:"tts_api_key"`
	STTBaseURL  string `json:"stt_base_url"`
	TTSBaseURL  string `json:"tts_base_url"`
	STTLanguage string `json:"stt_language"`
	TTSProvider string `json:"tts_provider"`
	TTSVoice    string `json:"tts_voice"`

	// optional
	DeviceID string `json:"device_id" validate:"required"`

	// AdminPassword is the plaintext password the operator picks at setup time.
	// Server bcrypts it into config.AdminPasswordHash and never persists the
	// plaintext. Used to gate browser admin access via POST /api/login + the
	// os_session cookie. Empty allowed (validated at handler level so
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
// ChannelWhatsapp is intentionally NOT handled here — setup falls back to
// telegram if whatsapp is somehow requested via this path.
func (r *SetupRequest) EffectiveChannel() string {
	if r.Channel == ChannelSlack {
		return ChannelSlack
	}
	if r.Channel == ChannelDiscord {
		return ChannelDiscord
	}
	return ChannelTelegram
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
	// channel type: "telegram", "slack", "discord" or "whatsapp"
	Channel string `json:"channel" validate:"required"`

	// telegram
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	// slack
	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`
	// SlackMode selects the Slack transport: "socket" (default, OpenClaw opens
	// outbound WSS to Slack — needs SlackAppToken) or "http" (OpenClaw listens
	// for POSTs forwarded from a public proxy — needs SlackSigningSecret).
	// HTTP mode is the message-loss-tolerant path: a public proxy
	// (bff-campaign-service) receives Slack events, fans out via MQTT to the
	// device's slack_event handler, which POSTs to localhost OpenClaw.
	SlackMode          string `json:"slack_mode,omitempty"`
	SlackSigningSecret string `json:"slack_signing_secret,omitempty"`
	SlackWebhookPath   string `json:"slack_webhook_path,omitempty"` // optional, defaults to /slack/events when SlackMode=http

	// discord
	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// whatsapp — bot login is handled interactively by the Baileys CLI; only
	// the operator's E.164 phone number (the permitted DM caller) ships here.
	WhatsappUserID string `json:"whatsapp_user_id"`
}

// RefreshChannelRequest carries the credentials needed to re-apply a channel's
// canonical config block via AgentGateway.RefreshChannelConfig. The caller
// (device.Service.RefreshChannelConfig) sources the creds from config.json on
// the device — refresh does NOT carry tokens over MQTT.
type RefreshChannelRequest struct {
	Channel string

	// slack
	SlackBotToken string
	SlackAppToken string
	SlackUserID   string
	// SlackMode selects the Slack transport — see AddChannelRequest.SlackMode for
	// semantics. Empty / "socket" preserves existing behaviour; "http" switches to
	// proxy mode and consults SlackSigningSecret / SlackWebhookPath.
	SlackMode          string
	SlackSigningSecret string
	SlackWebhookPath   string
}

// EffectiveSlackMode resolves SlackMode, defaulting to "socket" so unset
// payloads keep current behaviour (existing installs unaffected).
func (r *AddChannelRequest) EffectiveSlackMode() string {
	if r.SlackMode == "http" {
		return "http"
	}
	return "socket"
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram".
func (r *AddChannelRequest) EffectiveChannel() string {
	switch r.Channel {
	case ChannelSlack:
		return ChannelSlack
	case ChannelDiscord:
		return ChannelDiscord
	case ChannelWhatsapp:
		return ChannelWhatsapp
	}
	return ChannelTelegram
}

// ValidateChannel checks that the required fields for the selected channel are present.
func (r *AddChannelRequest) ValidateChannel() error {
	switch r.EffectiveChannel() {
	case ChannelSlack:
		if r.SlackBotToken == "" {
			return fmt.Errorf("slack_bot_token is required for slack channel")
		}
		switch r.EffectiveSlackMode() {
		case "http":
			if r.SlackSigningSecret == "" {
				return fmt.Errorf("slack_signing_secret is required for slack channel in http mode")
			}
			// SlackAppToken not used in HTTP mode (Socket Mode only).
		default: // "socket"
			if r.SlackAppToken == "" {
				return fmt.Errorf("slack_app_token is required for slack channel in socket mode")
			}
		}
	case ChannelDiscord:
		if r.DiscordBotToken == "" {
			return fmt.Errorf("discord_bot_token is required for discord channel")
		}
		if r.DiscordGuildID == "" {
			return fmt.Errorf("discord_guild_id is required for discord channel")
		}
		if r.DiscordUserID == "" {
			return fmt.Errorf("discord_user_id is required for discord channel")
		}
	case ChannelWhatsapp:
		if r.WhatsappUserID == "" {
			return fmt.Errorf("whatsapp_user_id is required for whatsapp channel")
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
	CommandInfo         = "info"
	CommandAddChannel   = "add_channel"
	CommandOTA          = "ota"
	CommandData         = "data"
	CommandWhatsappPair = "whatsapp_pair"

	// CommandSlackEvent is sent by the public Slack-events proxy (bff-campaign-service)
	// when Slack delivers an Events API POST for a workspace this device owns. Payload
	// is a verbatim forward of Slack's HTTP request body + signature headers; this
	// device POSTs them to the local OpenClaw gateway's /slack/events endpoint, which
	// re-verifies the Slack signature (we don't strip / re-sign in the proxy because
	// OpenClaw owns the signing-secret check by design).
	//
	// Wire format: {"cmd":"slack_event","event_id":"Ev123","body":"<raw JSON>",
	//               "headers":{"X-Slack-Signature":"v0=...","X-Slack-Request-Timestamp":"...",
	//                          "Content-Type":"application/json"}}
	//
	// Devices configured for socket-mode Slack will silently 404 on the local POST
	// (gateway has no /slack/events route in that mode) — proxy SHOULD route only to
	// devices the backend has flipped to slack_mode="http".
	CommandSlackEvent = "slack_event"

	// CommandSlackCommand is sent by the same Slack proxy (bff-campaign-service)
	// when Slack delivers a slash-command invocation (/openclaw, /new, ...) for a
	// workspace this device owns. It is forwarded and verified exactly like
	// CommandSlackEvent — POSTed verbatim to the local OpenClaw gateway's single
	// HTTP webhook (default http://127.0.0.1:18789/slack/events), which routes it
	// to the slash-command handler by body shape and replies to the user via the
	// command's response_url. The only wire differences from slack_event are the
	// urlencoded Content-Type the proxy sets in headers and that the event_id slot
	// carries Slack's trigger_id (slash commands have no event_id).
	//
	// Wire format: {"cmd":"slack_command","event_id":"<trigger_id>","body":"<raw urlencoded form>",
	//               "headers":{"X-Slack-Signature":"v0=...","X-Slack-Request-Timestamp":"...",
	//                          "Content-Type":"application/x-www-form-urlencoded"}}
	//
	// Like slack_event, only relevant for devices in slack_mode="http".
	CommandSlackCommand = "slack_command"
)

// Data kinds carried inside CommandData envelope.
const (
	KindTTSSet       = "tts.set"       // persist TTS voice/provider/language config
	KindTTSPreview   = "tts.preview"   // one-shot TTS preview, no config write
	KindDeviceRename = "device.rename" // rewrite IDENTITY.md Name (WatchIdentity picks up wake-words)
	KindOAuthSet     = "oauth.set"     // store/replace OAuth token for a provider
	KindOAuthRemove  = "oauth.remove"  // delete OAuth token for a provider
	KindRealtimeSet  = "realtime.set"  // persist realtime voice-agent config (provider/voice/reasoning…)

	// KindHermesSetup / KindPicoclawSetup switch the active agentic backend. The
	// kind itself names the target runtime — the worker (stand-to-earn-worker's
	// steoauthkind.HermesSetup / PicoclawSetup) publishes the backend-specific
	// kind instead of a generic envelope carrying a runtime field. Both funnel
	// into device.Service.UpdateAgentRuntime, which persists config.agent_runtime
	// then runs switch-runtime.sh (toggle systemd units + restart os-server so
	// agent/factory.go re-resolves the gateway). The device acks each on
	// fd_channel with the same kind. Replaces the former generic agent_runtime.set.
	KindHermesSetup   = "hermes.setup"
	KindPicoclawSetup = "picoclaw.setup"

	// AgentRuntimeOpenClaw / AgentRuntimeHermes / AgentRuntimePicoclaw are the
	// swappable agentic backends. Source of truth mirrored by
	// internal/agent/factory.go's resolver and /usr/local/bin/switch-runtime.
	AgentRuntimeOpenClaw = "openclaw"
	AgentRuntimeHermes   = "hermes"
	AgentRuntimePicoclaw = "picoclaw"

	KindSystemInfo    = "system.info"    // aggregate: versions + network + host
	KindSystemVersion = "system.version" // lamp + bootstrap + hal + openclaw versions
	KindSystemNetwork = "system.network" // wlan0 IP, MAC, SSID, gateway

	// KindSkillsInstall installs a role's skill bundle. Data: {"role":"<role>"}.
	KindSkillsInstall = "skills.install"

	// KindChannelRefreshConfig re-applies the canonical channels.<channel> block on
	// an already-onboarded device. Targets older customers whose openclaw.json
	// predates schema additions (e.g. the socketMode block, object-form streaming,
	// dmPolicy) — backend pushes this so the device rewrites the block using the
	// current applySlackChannelConfig writer without a full re-onboarding flow.
	//
	// Separate from add_channel by design: refresh is config-only (no plugin
	// install, no CLI bootstrap, no pairing). Today only channel:"slack" is
	// implemented; other channels return a distinct error code so the backend can
	// branch. Credentials are read from config.json on the device — they are NOT
	// carried in the payload.
	//
	// Flow:
	//   server → device : kind=channel.refresh_config data={channel}
	//   device → server : status=configuring → status=success data={channel, runtime}
	//                                        | status=failure error=<code>
	KindChannelRefreshConfig = "channel.refresh_config"
)

// Connector (MCP) data-kind prefixes. The connector code is the suffix, e.g.
// "connector.set.notion" / "connector.remove.github". dispatchData prefix-matches
// these before the exact-kind switch.
const (
	DataKindConnectorSetPrefix    = "connector.set."
	DataKindConnectorRemovePrefix = "connector.remove."
)

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
	case ChannelDiscord:
		req.DiscordBotToken, _ = cfg["bot_token"].(string)
		req.DiscordGuildID, _ = cfg["guild_id"].(string)
		req.DiscordUserID, _ = cfg["user_id"].(string)
	case ChannelSlack:
		req.SlackBotToken, _ = cfg["bot_token"].(string)
		req.SlackAppToken, _ = cfg["app_token"].(string)
		req.SlackUserID, _ = cfg["channel_id"].(string)
		// HTTP-mode proxy fields (optional; omitted payloads keep Socket Mode behaviour).
		req.SlackMode, _ = cfg["mode"].(string)
		req.SlackSigningSecret, _ = cfg["signing_secret"].(string)
		req.SlackWebhookPath, _ = cfg["webhook_path"].(string)
	case ChannelWhatsapp:
		req.WhatsappUserID, _ = cfg["user_id"].(string)
	default:
		req.TelegramBotToken, _ = cfg["bot_token"].(string)
		req.TelegramUserID, _ = cfg["chat_id"].(string)
	}
	return req
}

// MQTTAddChannelResponse extends MQTTInfoResponse with channel-specific fields for fd_channel.
//
// For non-whatsapp channels we publish exactly one message with Status=success|failure.
// For whatsapp the pairing flow is streamed: one message each for
// pairing_starting → pairing_qr (1+) → success | timeout | failure.
// PairingQR* fields are populated only on Status="pairing_qr"; the QR text is
// a multi-line Unicode-block rendering — see PairingQRFormat.
type MQTTAddChannelResponse struct {
	MQTTInfoResponse
	Channel          string `json:"channel"`
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	PairingQRText    string `json:"pairing_qr_text,omitempty"`
	PairingQRFormat  string `json:"pairing_qr_format,omitempty"`
	PairingQRSeq     int    `json:"pairing_qr_seq,omitempty"`
	PairingExpiresAt string `json:"pairing_expires_at,omitempty"`
}

// MQTTWhatsappPairCommand is the fa_channel payload for cmd:"whatsapp_pair".
// No fields today; reserved for future per-account selection.
type MQTTWhatsappPairCommand struct{}

// MQTTWhatsappPairResponse mirrors MQTTAddChannelResponse but for re-pair flows
// that don't re-bootstrap the channel. Same streaming shape:
// pairing_starting → pairing_qr (1+) → success | timeout | failure.
type MQTTWhatsappPairResponse struct {
	MQTTInfoResponse
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	PairingQRText    string `json:"pairing_qr_text,omitempty"`
	PairingQRFormat  string `json:"pairing_qr_format,omitempty"`
	PairingQRSeq     int    `json:"pairing_qr_seq,omitempty"`
	PairingExpiresAt string `json:"pairing_expires_at,omitempty"`
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
	HalVersion      string `json:"hal_version,omitempty"`
	OpenClawVersion string `json:"openclaw_version,omitempty"`
	LocalIP         string `json:"local_ip,omitempty"`
}

// NewDeviceMessage creates a base message with required fields populated from config.
func NewMQTTInfoResponse(cfg *config.Config, msgType string, mac string) MQTTInfoResponse {
	return MQTTInfoResponse{
		Device:      cfg.DeviceTypeOrDefault(),
		Type:        msgType,
		Version:     config.OSVersion,
		ID:          cfg.DeviceID,
		Mac:         mac,
		Time:        time.Now().UTC().Format(time.RFC3339Nano),
		TTSProvider: cfg.TTSProvider,
		TTSVoice:    cfg.TTSVoice,
		STTLanguage: cfg.STTLanguage,
	}
}

// MQTTDataCommand is the fa_channel payload for cmd:"data" — a generic envelope.
// Sub-handlers branch on Kind and unmarshal Data into a kind-specific struct.
//
// Type selects the delivery path:
//   - "" (default)  → Data is inline; dispatch immediately.
//   - "privacy"     → Data is omitted on the broker and lives on the backend;
//     the device acks "received" then fetches it over TLS from
//     /devices/get-message before dispatching (see privacy_fetch.go).
type MQTTDataCommand struct {
	Kind string          `json:"kind"`
	Type string          `json:"type,omitempty"`
	Data json.RawMessage `json:"data"`
}

// MQTT data delivery types and statuses for the privacy envelope flow.
const (
	// MQTTDataTypePrivacy marks an envelope whose Data block must be fetched
	// from the backend instead of read inline — keeps secrets off the broker.
	MQTTDataTypePrivacy = "privacy"
	// MQTTStatusReceived is the ack the device publishes the moment it accepts
	// a privacy envelope, before the async backend fetch begins. Tells the
	// backend "got it, stop retrying" without being the terminal status.
	MQTTStatusReceived = "received"
)

// MQTTDataResponse is the fd_channel reply for cmd:"data".
// Echoes Kind so the server can correlate with its outbound request.
type MQTTDataResponse struct {
	MQTTInfoResponse
	Kind   string      `json:"kind"`
	Status string      `json:"status"`
	Error  string      `json:"error,omitempty"`
	Data   interface{} `json:"data,omitempty"`
}

// MQTTSystemInfoData is the response payload for kind:"system.info" — an
// aggregate snapshot of versions + network + host. Fields are zero-valued when
// the probe fails (e.g. openclaw not yet installed → OpenClaw="", OpenClawDetected=false).
type MQTTSystemInfoData struct {
	Versions MQTTVersionsData `json:"versions"`
	Network  MQTTNetworkData  `json:"network"`
	Host     MQTTHostData     `json:"host"`
}

// MQTTVersionsData carries the component version strings on the device.
// Empty string means probing failed; OpenClawDetected lets the caller
// distinguish "not installed" from "installed but unparseable".
type MQTTVersionsData struct {
	OSServer         string `json:"os-server"`
	Bootstrap        string `json:"bootstrap"`
	Hal              string `json:"hal"`
	OpenClaw         string `json:"openclaw"`
	OpenClawDetected bool   `json:"openclaw_detected"`
}

// MQTTNetworkData carries wlan0 link facts. SSID/Gateway empty when the device
// is in AP mode or otherwise not joined to upstream Wi-Fi.
type MQTTNetworkData struct {
	PrivateIP string `json:"private_ip"`
	Interface string `json:"interface"`
	MAC       string `json:"mac"`
	SSID      string `json:"ssid"`
	Gateway   string `json:"gateway"`
}

// MQTTHostData carries host-process facts useful for ops dashboards.
type MQTTHostData struct {
	Hostname      string `json:"hostname"`
	DeviceID      string `json:"device_id"`
	DeviceName    string `json:"device_name"` // friendly "<device_type>-xxxx"
	UptimeSeconds int64  `json:"uptime_seconds"`
}

// MQTTOAuthSetData is the Data payload for kind:"oauth.set".
// Provider is a free-form key (e.g. "google", "twitter", "github") used as the
// map key in access_tokens.json.
type MQTTOAuthSetData struct {
	Provider     string   `json:"provider"`
	AccessToken  string   `json:"access_token"`
	RefreshToken string   `json:"refresh_token,omitempty"`
	TokenType    string   `json:"token_type,omitempty"`
	ExpiresAt    int64    `json:"expires_at,omitempty"` // unix seconds; 0 = never expires
	Scopes       []string `json:"scopes,omitempty"`
	UserEmail    string   `json:"user_email,omitempty"`
	ClientID     string   `json:"client_id,omitempty"`
}

// MQTTOAuthRemoveData is the Data payload for kind:"oauth.remove".
type MQTTOAuthRemoveData struct {
	Provider string `json:"provider"`
}

// MQTTChannelRefreshConfigData is the Data payload for kind:"channel.refresh_config".
// Channel selects which channels.<channel> block to re-apply. Today only "slack"
// is implemented; other channels return an error. Credentials are read from
// config.json on the device — they are NOT carried in this payload.
type MQTTChannelRefreshConfigData struct {
	Channel string `json:"channel"`
}

// MQTTChannelRefreshConfigResultData is the Data payload echoed in fd_channel
// success/failure messages for kind:"channel.refresh_config". Runtime carries
// the detected openclaw runtime version string (empty if probing failed) so the
// backend can correlate refresh outcomes with runtime upgrades.
type MQTTChannelRefreshConfigResultData struct {
	Channel string `json:"channel"`
	Runtime string `json:"runtime,omitempty"`
}

// OAuthTokenEntry is the on-disk representation of a single provider's token
// inside access_tokens.json.
type OAuthTokenEntry struct {
	AccessToken    string   `json:"access_token"`
	RefreshToken   string   `json:"refresh_token,omitempty"`
	TokenType      string   `json:"token_type,omitempty"`
	ExpiresAt      int64    `json:"expires_at,omitempty"`
	Scopes         []string `json:"scopes,omitempty"`
	UserEmail      string   `json:"user_email,omitempty"`
	ClientID       string   `json:"client_id,omitempty"`
	ObtainedAt     int64    `json:"obtained_at"`               // unix seconds when this device received the token
	RefreshRevoked bool     `json:"refresh_revoked,omitempty"` // set when the backend returned invalid_grant — skip until user re-auths
}

// AccessTokensFile is the on-disk schema for workspace/configs/access_tokens.json.
type AccessTokensFile struct {
	Version   int                        `json:"version"`
	Providers map[string]OAuthTokenEntry `json:"providers"`
}

// MQTTConnectorSetData is the Data payload for kind:"connector.set.<code>".
// The backend drives the OAuth/app flow and pushes the resulting credentials
// here; the device writes the token file + the mcp.servers.<code> entry into
// openclaw.json. ExpiresIn (seconds-from-now) is normalized to an absolute
// ExpiresAt on store.
type MQTTConnectorSetData struct {
	Connector    string `json:"connector"`
	AuthType     string `json:"auth_type"`
	AccessToken  string `json:"access_token,omitempty"`
	RefreshToken string `json:"refresh_token,omitempty"`
	TokenType    string `json:"token_type,omitempty"`
	ExpiresIn    int    `json:"expires_in,omitempty"` // seconds from now
	ExpiresAt    int64  `json:"expires_at,omitempty"` // unix seconds (wins over expires_in)
	// APIKey carries the credential for static-API-key connectors (e.g. Ahrefs)
	// whose auth_type is not OAuth — the key lands here, not in access_token.
	APIKey    string   `json:"api_key,omitempty"`
	Scopes    []string `json:"scopes,omitempty"`
	UserEmail string   `json:"user_email,omitempty"`
	ClientID  string   `json:"client_id,omitempty"`
	// Credentials holds connector-specific extras the backend wants persisted
	// verbatim (preserved across token refreshes).
	Credentials map[string]string `json:"credentials,omitempty"`
	// Refresh gates the connector refresh loop: only entries with refresh:true
	// AND a refresh_token are auto-rotated. Backend is the source of truth.
	Refresh bool `json:"refresh,omitempty"`
}

// MQTTConnectorRemoveData is the Data payload for kind:"connector.remove.<code>".
type MQTTConnectorRemoveData struct {
	Connector string `json:"connector"`
}

// ConnectorEntry is the on-disk representation of a single connector's
// credentials inside workspace/configs/connectors.json.
type ConnectorEntry struct {
	AuthType     string            `json:"auth_type,omitempty"`
	AccessToken  string            `json:"access_token"`
	RefreshToken string            `json:"refresh_token,omitempty"`
	TokenType    string            `json:"token_type,omitempty"`
	ExpiresAt    int64             `json:"expires_at,omitempty"`
	APIKey       string            `json:"api_key,omitempty"`
	Scopes       []string          `json:"scopes,omitempty"`
	UserEmail    string            `json:"user_email,omitempty"`
	ClientID     string            `json:"client_id,omitempty"`
	Credentials  map[string]string `json:"credentials,omitempty"`
	Refresh      bool              `json:"refresh,omitempty"`
	ObtainedAt   int64             `json:"obtained_at"`
}

// ConnectorsFile is the on-disk schema for workspace/configs/connectors.json.
type ConnectorsFile struct {
	Version    int                       `json:"version"`
	Connectors map[string]ConnectorEntry `json:"connectors"`
}

// MQTTSkillsInstallData is the Data payload for kind:"skills.install".
// Role is a free-form slug owned by the backend catalog; the device fetches
// <role>/skills.zip on demand.
type MQTTSkillsInstallData struct {
	Role string `json:"role"`
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

// ===========================================================================
// Configure the realtime voice agent (Gemini Live / OpenAI Realtime) from the
// backend / web dashboard. The payload (RealtimeSetData) is shared by TWO
// transports — pick whichever fits:
//   • MQTT  — `realtime.set` downlink (envelope below). Async ack on fd_channel.
//   • HTTP  — POST the device config endpoint with a `"realtime"` object holding
//             the SAME fields: {"realtime": { ...RealtimeSetData... }} (rides the
//             existing UpdateConfig route, exactly like tts_provider/stt_language).
// Both paths validate, write the `realtime` block to config.json, and restart HAL.
//
// HOW TO PUSH over MQTT (for FE / BFF teams)
// --------------------------------
// Publish a DOWNLINK to the device's command channel (the same `fa_channel`
// topic tts.set uses — i.e. the topic the device subscribes to). Envelope:
//
//	{ "cmd": "data", "kind": "realtime.set", "data": { ...RealtimeSetData... } }
//
// The device replies on its `fd_channel` (uplink) with MQTTRealtimeSetAck:
//   1) {"kind":"realtime.set","status":"starting"}            — received
//   2) {"kind":"realtime.set","status":"success","data":{…}}  — applied, OR
//      {"kind":"realtime.set","status":"failure","error":"…"} — rejected
//
// EFFECT: the device writes the values into the `realtime` block of its
// config.json and RESTARTS HAL (takes a few seconds). HAL then reads the new
// block on boot. So `success` means "saved + hal restarting", not "live yet".
//
// FIELD SEMANTICS (all fields optional; omit a field = leave it unchanged)
//   enabled   bool   — turn the realtime brain on/off. false = off (device
//                      falls back to the classic STT→agent→TTS path).
//   provider  string — "gemini" | "openai" | "none". "none" = off.
//   model     string — applied to the ACTIVE provider (the one in `provider`,
//                      or the current provider if `provider` is omitted).
//   voice     string — active provider's voice. Valid:
//                        gemini: Puck | Charon | Kore | Fenrir | Aoede
//                        openai: alloy | ash | coral | echo | fable | onyx |
//                                nova | sage | shimmer
//   reasoning string — active provider's reasoning depth (cost knob). Valid:
//                        gemini (thinking_level): MINIMAL | LOW | MEDIUM | HIGH
//                        openai (reasoning_effort): minimal|low|medium|high|xhigh
//                      Defaults are the CHEAPEST tier (MINIMAL / minimal).
//   api_key   string — override the realtime provider key. Empty/omitted →
//                      falls back to the device's llm_api_key.
//   base_url  string — override the realtime endpoint. Empty/omitted → derived
//                      from llm_base_url (…/ws/gemini or …/ws/openai).
//
// RULES
//   - When any of model/voice/reasoning is sent, `provider` (or the current
//     provider) MUST be a concrete gemini|openai — those knobs are per-provider.
//   - Invalid provider/voice/reasoning → status:"failure" with a descriptive
//     error; nothing is written.
//
// EXAMPLES
//   Switch to OpenAI Realtime with a voice:
//     {"cmd":"data","kind":"realtime.set","data":{"provider":"openai","voice":"alloy"}}
//   Tune Gemini reasoning up (more expensive):
//     {"cmd":"data","kind":"realtime.set","data":{"provider":"gemini","reasoning":"HIGH"}}
//   Turn realtime off:
//     {"cmd":"data","kind":"realtime.set","data":{"enabled":false}}
// ===========================================================================

// RealtimeSetData is the realtime-config payload, shared by the MQTT
// `realtime.set` downlink (data block) and the HTTP UpdateConfig `realtime` field.
type RealtimeSetData struct {
	Enabled   *bool  `json:"enabled,omitempty"`   // nil = leave unchanged
	Provider  string `json:"provider,omitempty"`  // gemini | openai | none
	Model     string `json:"model,omitempty"`     // active provider's model
	Voice     string `json:"voice,omitempty"`     // active provider's voice
	Reasoning string `json:"reasoning,omitempty"` // gemini thinking_level OR openai reasoning_effort
	APIKey    string `json:"api_key,omitempty"`   // optional override; empty → llm_api_key
	BaseURL   string `json:"base_url,omitempty"`  // optional override; empty → llm_base_url-derived
}

// MQTTRealtimeSetCommand wraps the full realtime.set downlink envelope for unmarshalling.
type MQTTRealtimeSetCommand struct {
	Data RealtimeSetData `json:"data"`
}

// MQTTRealtimeSetAck is published to fd_channel after applying (or failing) a
// realtime.set downlink. status: "starting" | "success" | "failure".
type MQTTRealtimeSetAck struct {
	MQTTInfoResponse
	Kind   string           `json:"kind"`
	Status string           `json:"status"`
	Error  string           `json:"error,omitempty"`
	Data   *RealtimeSetData `json:"data,omitempty"`
}

// AgentRuntimeSetData carries the target backend for a runtime switch. The MQTT
// path no longer reads Runtime off the wire — the hermes.setup / picoclaw.setup
// kind names the target — but it is still the request body for the HTTP
// POST /api/device/agent-runtime path and the value persisted to config. The
// valid set mirrors agent/factory.go's resolver; anything else is rejected (we
// don't silently fall back here — an unknown value from the BFF is a contract
// error, not a default).
//
//	{ "cmd": "data", "kind": "hermes.setup" }    // switch to hermes
//	{ "cmd": "data", "kind": "picoclaw.setup" }  // switch to picoclaw
type AgentRuntimeSetData struct {
	Runtime string `json:"runtime"` // "openclaw" | "hermes" | "picoclaw"
}

// AgentRuntimes is the valid set, surfaced to the web settings dropdown via
// GET /api/device/agent-runtime so the UI never hardcodes the list.
var AgentRuntimes = []string{AgentRuntimeOpenClaw, AgentRuntimeHermes, AgentRuntimePicoclaw}

// AgentRuntimeStatus is returned by GET /api/device/agent-runtime: the active
// backend plus the selectable options.
type AgentRuntimeStatus struct {
	Current string   `json:"current"`
	Options []string `json:"options"`
}

// AgentRuntimeSetAck is published to fd_channel after applying (or failing) a
// hermes.setup / picoclaw.setup downlink — Kind echoes the triggering kind so
// the worker can match it. status: "starting" | "success" | "failure". On
// "success" the device restarts os-server, so the BFF should expect a brief
// reconnect — the new banner (AGENT BACKEND ACTIVE) confirms the swap landed.
type AgentRuntimeSetAck struct {
	MQTTInfoResponse
	Kind   string               `json:"kind"`
	Status string               `json:"status"`
	Error  string               `json:"error,omitempty"`
	Data   *AgentRuntimeSetData `json:"data,omitempty"`
}

// MQTTTTSPreviewData is the nested data payload for cmd:"data", kind:"tts.preview".
// Text is required; Provider/Voice/Language are optional overrides — empty
// fields make HAL fall back to the device's current TTS config.
type MQTTTTSPreviewData struct {
	Text     string `json:"text"`
	Provider string `json:"provider,omitempty"`
	Voice    string `json:"voice,omitempty"`
	Language string `json:"language,omitempty"`
}

// MQTTTTSPreviewCommand wraps the full tts.preview downlink envelope for unmarshalling.
type MQTTTTSPreviewCommand struct {
	Data MQTTTTSPreviewData `json:"data"`
}

// MQTTDeviceRenameData is the nested data payload for cmd:"data", kind:"device.rename".
// Name is the new agent name written into workspace/IDENTITY.md's **Name:** line.
// WatchIdentity picks up the change within 5s and pushes new wake words to HAL;
// OpenClaw re-reads IDENTITY.md on its own — no gateway restart needed.
type MQTTDeviceRenameData struct {
	Name string `json:"name"`
}

// ConfigPublicResponse is returned by GET /api/device/config. Raw secrets
// (API keys, channel tokens, MQTT/WiFi passwords) are replaced by boolean
// presence flags so the web UI can render "configured ✓" + a write-only
// SecretUpdateField. Non-secret fields (URLs, IDs, model name, language)
// are returned as-is because they're useful for the UI and not sensitive.
// RealtimePublic is the read-back view of the realtime voice-agent config — the
// RESOLVED active-provider values (provider/model/voice/reasoning for whichever
// provider is active, enabled state, resolved base_url). Mirrors how
// tts_provider/tts_voice are surfaced; the key is exposed only as HasAPIKey.
type RealtimePublic struct {
	Enabled   bool   `json:"enabled"`
	Provider  string `json:"provider"` // "" when realtime is off
	Model     string `json:"model"`
	Voice     string `json:"voice"`
	Reasoning string `json:"reasoning"`
	BaseURL   string `json:"base_url"` // resolved (may be llm-derived)
	HasAPIKey bool   `json:"has_api_key"`
}

type ConfigPublicResponse struct {
	Channel            string `json:"channel"`
	TelegramUserID     string `json:"telegram_user_id"`
	SlackUserID        string `json:"slack_user_id"`
	DiscordGuildID     string `json:"discord_guild_id"`
	DiscordUserID      string `json:"discord_user_id"`
	WhatsappUserID     string `json:"whatsapp_user_id"`
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

	// Realtime voice-agent config — RESOLVED active-provider values for the web to
	// render the form. Write back via UpdateConfig's `realtime` field. The api_key
	// is never returned (only HasAPIKey, the realtime-specific override; the LLM
	// key fallback is reported by HasLLMAPIKey).
	Realtime RealtimePublic `json:"realtime"`

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

	WhatsappUserID string `json:"whatsapp_user_id"`

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

	// Realtime voice-agent config (Gemini Live / OpenAI Realtime). Same payload
	// as the MQTT realtime.set downlink; omit to leave the realtime block alone.
	// See RealtimeSetData for field semantics + valid values.
	Realtime *RealtimeSetData `json:"realtime,omitempty"`

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
