package device

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/beclient"
	"go.autonomous.ai/os/internal/network"
	"go.autonomous.ai/os/lib/i18n"
	"go.autonomous.ai/os/server/config"
)

// normalizeBaseURL ensures autonomous.ai base URLs include the /v1 OpenAI-compat
// prefix so all backends (TTS, STT, LLM) receive a ready-to-use URL without each
// backend having to patch it individually. Non-autonomous URLs are left untouched.
func normalizeBaseURL(base string) string {
	base = strings.TrimSuffix(strings.TrimSpace(base), "/")
	if strings.Contains(base, "campaign-api.autonomous.ai") && strings.HasSuffix(base, "/ai") {
		base += "/v1"
	}
	return base
}

// Setup phase strings exposed via /api/setup/status so the web client can
// follow the device through the AP→STA transition. Phases progress only
// forward; failures park at "failed".
const (
	SetupPhaseIdle       = "idle"
	SetupPhaseConnecting = "connecting"
	SetupPhaseConnected  = "connected"
	SetupPhaseFailed     = "failed"
)

// apSetupIP is wlan0's static address while the device runs the provisioning
// AP (see scripts/provision/setup-ap.sh). The early LAN-IP poll skips it so it
// only ever publishes the STA-side address from the home router's DHCP.
const apSetupIP = "192.168.100.1"

type setupState struct {
	mu    sync.RWMutex
	phase string
	lanIP string
	error string
}

func (st *setupState) snapshot() (phase, ip, errMsg string) {
	st.mu.RLock()
	defer st.mu.RUnlock()
	return st.phase, st.lanIP, st.error
}

func (st *setupState) set(phase, ip, errMsg string) {
	st.mu.Lock()
	st.phase = phase
	st.lanIP = ip
	st.error = errMsg
	st.mu.Unlock()
}

// ErrSlackCredentialsMissing is returned by RefreshChannelConfig when the
// device's config.json has no slack bot token — refresh cannot synthesize it, so
// the caller must run /api/device/setup or add_channel first.
var ErrSlackCredentialsMissing = errors.New("slack_credentials_missing")

// ErrChannelNotSupported is returned by RefreshChannelConfig for channels that
// cannot be refreshed in a config-only way (or unknown channel names). Today
// only slack is wired into the refresh path.
var ErrChannelNotSupported = errors.New("channel_not_supported")

type Service struct {
	config         *config.Config
	networkService *network.Service
	agentGateway   domain.AgentGateway
	beClient       *beclient.Client
	setupState     setupState
}

func ProvideService(config *config.Config, ns *network.Service, gw domain.AgentGateway, be *beclient.Client) *Service {
	return &Service{
		config:         config,
		networkService: ns,
		agentGateway:   gw,
		beClient:       be,
		setupState:     setupState{phase: SetupPhaseIdle},
	}
}

// SetupStatus returns the current Setup phase + LAN IP so the web client
// can poll progress through the AP→STA switch. When no Setup run has
// happened (phase=idle) but the device is already on home Wi-Fi from a
// previous session, fall back to the live wlan0 address so the web
// client can still detect "you're at the AP IP but the device lives at X"
// and redirect.
func (s *Service) SetupStatus() (phase, lanIP, errMsg string) {
	phase, lanIP, errMsg = s.setupState.snapshot()
	if lanIP == "" {
		if ip, err := s.networkService.GetCurrentIP(); err == nil {
			lanIP = ip
		}
	}
	return phase, lanIP, errMsg
}

func (s *Service) Setup(data domain.SetupRequest) error {
	slog.Info("starting setup", "component", "device")
	data.LLMBaseURL = normalizeBaseURL(data.LLMBaseURL)
	data.STTBaseURL = normalizeBaseURL(data.STTBaseURL)
	data.TTSBaseURL = normalizeBaseURL(data.TTSBaseURL)
	s.setupState.set(SetupPhaseConnecting, "", "")

	// Early LAN-IP capture: SetupNetwork() blocks up to 60s waiting for
	// internet, but the AP (192.168.100.1) tears down within ~2s of the
	// AP→STA switch — so by the time SetupNetwork returns and we'd normally
	// read the IP, the web client can no longer poll us over the AP. This
	// goroutine polls wlan0 while SetupNetwork runs and publishes the new STA
	// IP into setupState the instant it appears (before internet is even up),
	// giving the FE the largest possible window to read lan_ip during the
	// brief overlap where it's still polling. Phase stays "connecting" — a
	// LAN IP alone doesn't prove the join fully succeeded; SetupNetwork's
	// return flips it to connected/failed below.
	ipPollDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ipPollDone:
				return
			case <-ticker.C:
				ip, ipErr := s.networkService.GetCurrentIP()
				// Ignore the AP's own static IP — we want the STA-side
				// address handed out by the home router's DHCP.
				if ipErr == nil && ip != "" && ip != apSetupIP {
					if _, prevIP, _ := s.setupState.snapshot(); prevIP != ip {
						s.setupState.set(SetupPhaseConnecting, ip, "")
						slog.Info("setup: early LAN IP captured", "component", "device", "lan_ip", ip)
					}
				}
			}
		}
	}()

	result, err := s.networkService.SetupNetwork(data.SSID, data.Password)
	close(ipPollDone)
	if err != nil {
		s.setupState.set(SetupPhaseFailed, "", err.Error())
		return fmt.Errorf("setup network: %w", err)
	}
	if !result {
		s.setupState.set(SetupPhaseFailed, "", "network setup failed")
		return fmt.Errorf("network setup failed")
	}
	// Capture the LAN IP immediately after WiFi associates so the web
	// client polling /api/setup/status can read it before AP shuts down.
	// Re-reading here can fail transiently while the AP tears down — in that
	// case keep whatever the early-capture goroutine already published rather
	// than clobbering a good IP with an empty string.
	ip, ipErr := s.networkService.GetCurrentIP()
	if ipErr != nil || ip == "" || ip == apSetupIP {
		_, prevIP, _ := s.setupState.snapshot()
		ip = prevIP
	}
	if ip != "" {
		s.setupState.set(SetupPhaseConnected, ip, "")
		slog.Info("setup: WiFi associated", "component", "device", "lan_ip", ip)
	} else {
		s.setupState.set(SetupPhaseConnected, "", "")
		slog.Warn("setup: WiFi associated but no IP detected", "component", "device", "error", ipErr)
	}

	// Persist the user's model selection so SetupAgent (run below, AFTER the full
	// config is saved) can fall back to it when the model API is unreachable.
	s.config.LLMModel = data.LLMModel

	llmAPIKey := data.LLMAPIKey
	llmBaseURL := data.LLMBaseURL
	channel := data.EffectiveChannel()

	s.config.LLMAPIKey = llmAPIKey
	s.config.LLMBaseURL = llmBaseURL
	// LLMModel already set above (and possibly overridden by SetupAgent from the
	// upstream default_model). Do not re-assign it from the raw request here.
	s.config.Channel = channel
	switch channel {
	case "slack":
		s.config.SlackBotToken = data.SlackBotToken
		s.config.SlackAppToken = data.SlackAppToken
		s.config.SlackUserID = data.SlackUserID
	case "discord":
		s.config.DiscordBotToken = data.DiscordBotToken
		s.config.DiscordUserID = data.DiscordUserID
	default:
		s.config.TelegramBotToken = data.TelegramBotToken
		s.config.TelegramUserID = data.TelegramUserID
	}
	s.config.DeviceID = data.DeviceID
	s.config.DeepgramAPIKey = data.DeepgramAPIKey
	s.config.STTAPIKey = data.STTAPIKey
	s.config.TTSAPIKey = data.TTSAPIKey
	s.config.STTBaseURL = data.STTBaseURL
	s.config.TTSBaseURL = data.TTSBaseURL
	s.config.STTLanguage = data.STTLanguage
	s.config.STTModel = sttModelForLanguage(data.STTLanguage)
	if data.TTSProvider != "" {
		s.config.TTSProvider = data.TTSProvider
	}
	if data.TTSVoice != "" {
		s.config.TTSVoice = data.TTSVoice
	}
	s.config.MQTTEndpoint = data.MQTTEndpoint
	s.config.MQTTUsername = data.MQTTUsername
	s.config.MQTTPassword = data.MQTTPassword
	s.config.MQTTPort = data.MQTTPort
	s.config.FAChannel = data.FAChannel
	s.config.FDChannel = data.FDChannel
	if data.LLMDisableThinking != nil {
		s.config.LLMDisableThinking = data.LLMDisableThinking
	}
	// Admin password is hashed once and never persisted in plaintext. Empty
	// is permitted so older clients that don't send it still complete setup;
	// the operator can set one later via PUT /api/device/config (TODO) or
	// re-run setup after factory reset.
	if data.AdminPassword != "" {
		hash, hashErr := bcrypt.GenerateFromPassword([]byte(data.AdminPassword), bcrypt.DefaultCost)
		if hashErr != nil {
			return fmt.Errorf("hash admin password: %w", hashErr)
		}
		s.config.AdminPasswordHash = string(hash)
	}
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}
	slog.Info("config saved", "component", "device")

	// SetupAgent runs AFTER config.json is saved: a backend that materializes its
	// own config from config.json (Hermes presync) then sees the freshly-entered
	// llm_api_key/base_url + channel tokens immediately, instead of waiting for the
	// next os-server boot. OpenClaw writes openclaw.json from the request `data`, so
	// its result is unchanged; any LLMModel override it applies is persisted by the
	// SetUpCompleted save below.
	if err := s.agentGateway.SetupAgent(data); err != nil {
		return err
	}

	// Wait for agent gateway to be ready before marking device as working.
	if ok := s.WaitForAgentReady(120 * time.Second); !ok {
		return fmt.Errorf("agent gateway ready timeout, something went wrong")
	}

	s.config.SetUpCompleted = true
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}

	slog.Info("agent gateway is ready", "component", "device")
	if s.beClient != nil && llmAPIKey != "" {
		s.beClient.PingSafe(llmAPIKey, beclient.PingPayload{
			Status:         "working",
			SetupCompleted: true,
			Mac:            GetDeviceMac(),
			Version:        config.OSVersion,
		})
	}
	return nil
}

// AddChannel adds a messaging channel to the agent without re-running full setup.
//
// For non-whatsapp channels the call is synchronous and the returned channel is
// nil — callers should publish a single success/failure response after this
// returns. For whatsapp the call returns a streaming event channel
// (pairing_starting → pairing_qr* → success | timeout | failure); the channel
// is closed when the flow terminates. Callers MUST drain. `success` is emitted
// both for first-time pairing and for resumed sessions (creds already on
// disk).
func (s *Service) AddChannel(ctx context.Context, data domain.AddChannelRequest) (<-chan domain.PairingEvent, error) {
	if err := s.agentGateway.AddChannel(ctx, data); err != nil {
		return nil, fmt.Errorf("add channel in agent: %w", err)
	}

	channel := data.EffectiveChannel()
	s.config.Channel = channel
	switch channel {
	case domain.ChannelSlack:
		s.config.SlackBotToken = data.SlackBotToken
		s.config.SlackAppToken = data.SlackAppToken
		s.config.SlackUserID = data.SlackUserID
	case domain.ChannelDiscord:
		s.config.DiscordBotToken = data.DiscordBotToken
		s.config.DiscordUserID = data.DiscordUserID
	case domain.ChannelWhatsapp:
		s.config.WhatsappUserID = data.WhatsappUserID
	default:
		s.config.TelegramBotToken = data.TelegramBotToken
		s.config.TelegramUserID = data.TelegramUserID
	}
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}
	slog.Info("added channel", "component", "device", "channel", channel)

	if channel != domain.ChannelWhatsapp {
		return nil, nil
	}
	// Existing Baileys creds on disk → no QR needed; emit a single success
	// event so the caller's drain loop sees the same terminal status it would
	// for a first-time pair.
	if s.agentGateway.HasWhatsappSession("default") {
		slog.Info("existing whatsapp session detected, skipping pairing", "component", "device")
		ch := make(chan domain.PairingEvent, 1)
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
		close(ch)
		return ch, nil
	}
	return s.agentGateway.PairWhatsapp(ctx), nil
}

// RefreshChannelConfig re-applies the canonical channel config block to
// openclaw.json on the device. Triggered by the channel.refresh_config MQTT kind
// to fix older devices whose config predates schema additions (e.g. the
// socketMode block, object-form streaming, dmPolicy).
//
// Reads credentials from config.json (set previously by /api/device/setup or
// add_channel) — refresh does NOT carry tokens over MQTT. Delegates the
// write+restart to AgentGateway.RefreshChannelConfig, the separate
// non-AddChannel code path so the two flows can diverge cleanly.
//
// Returns the detected runtime version string ("Y.M.P", empty when undetected)
// and sentinel errors the MQTT handler maps to stable status codes:
//   - ErrSlackCredentialsMissing — config.json has no slack bot token
//   - ErrChannelNotSupported     — unknown channel or one not wired into refresh
func (s *Service) RefreshChannelConfig(ctx context.Context, channel string) (string, error) {
	switch channel {
	case domain.ChannelSlack:
		// Bot token is the one mandatory credential for both transports.
		// AppToken is socket-mode-only — refresh succeeds without it when
		// migrating to HTTP mode (signing_secret comes from LLMAPIKey instead,
		// which the device always has).
		if s.config.SlackBotToken == "" {
			return "", ErrSlackCredentialsMissing
		}
		// Refresh defaults to HTTP mode: use the device's llm_api_key (LLMAPIKey
		// on disk) as the signingSecret so it matches what the backend proxy
		// re-signs with. Socket-mode installs flip to HTTP the first time the
		// backend sends channel.refresh_config — no per-device add_channel push.
		return s.agentGateway.RefreshChannelConfig(ctx, domain.RefreshChannelRequest{
			Channel:            channel,
			SlackBotToken:      s.config.SlackBotToken,
			SlackAppToken:      s.config.SlackAppToken, // ignored in http mode, kept for back-compat
			SlackUserID:        s.config.SlackUserID,
			SlackMode:          "http",
			SlackSigningSecret: s.config.LLMAPIKey,
		})
	default:
		return "", ErrChannelNotSupported
	}
}

// PairWhatsapp re-runs the WhatsApp Linked Devices pairing flow without
// re-bootstrapping the channel config. Used by the whatsapp_pair MQTT command
// for re-pair after session loss.
func (s *Service) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	return s.agentGateway.PairWhatsapp(ctx)
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
			resp := s.beClient.PingSafe(s.config.LLMAPIKey, beclient.PingPayload{
				Status:         "working",
				SetupCompleted: s.config.SetUpCompleted,
				Mac:            GetDeviceMac(),
				Version:        config.OSVersion,
				SlackTeamID:    s.beClient.SlackTeamID(),
			})
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

// GetPublicConfig returns the device configuration with secrets replaced by
// presence booleans, suitable for browser bootstrap. The web UI renders
// write-only fields against the `Has*` flags so plaintext tokens never reach
// the DOM / sessionStorage / HAR captures.
func (s *Service) GetPublicConfig() domain.ConfigPublicResponse {
	disableThinking := false
	if s.config.LLMDisableThinking != nil {
		disableThinking = *s.config.LLMDisableThinking
	}
	deviceID := s.config.DeviceID
	if deviceID == "" {
		deviceID = GetDeviceMac()
	}
	return domain.ConfigPublicResponse{
		Channel:            s.config.Channel,
		TelegramUserID:     s.config.TelegramUserID,
		SlackUserID:        s.config.SlackUserID,
		DiscordGuildID:     s.config.DiscordGuildID,
		DiscordUserID:      s.config.DiscordUserID,
		WhatsappUserID:     s.config.WhatsappUserID,
		LLMModel:           s.config.LLMModel,
		LLMBaseURL:         s.config.LLMBaseURL,
		LLMDisableThinking: disableThinking,
		STTBaseURL:         s.config.STTBaseURL,
		TTSBaseURL:         s.config.TTSBaseURL,
		STTLanguage:        s.config.STTLanguage,
		STTModel:           s.config.STTModel,
		TTSProvider:        s.config.TTSProvider,
		TTSVoice:           s.config.TTSVoice,
		DeviceID:           deviceID,
		Mac:                GetDeviceMac(),
		NetworkSSID:        s.config.NetworkSSID,
		MQTTEndpoint:       s.config.MQTTEndpoint,
		MQTTUsername:       s.config.MQTTUsername,
		MQTTPort:           s.config.MQTTPort,
		FAChannel:          s.config.FAChannel,
		FDChannel:          s.config.FDChannel,

		HasTelegramBotToken: s.config.TelegramBotToken != "",
		HasSlackBotToken:    s.config.SlackBotToken != "",
		HasSlackAppToken:    s.config.SlackAppToken != "",
		HasDiscordBotToken:  s.config.DiscordBotToken != "",
		HasLLMAPIKey:        s.config.LLMAPIKey != "",
		HasDeepgramAPIKey:   s.config.DeepgramAPIKey != "",
		HasSTTAPIKey:        s.config.STTAPIKey != "",
		HasTTSAPIKey:        s.config.TTSAPIKey != "",
		HasNetworkPassword:  s.config.NetworkPassword != "",
		HasMQTTPassword:     s.config.MQTTPassword != "",
		HasAdminPassword:    s.config.AdminPasswordHash != "",
		Realtime: domain.RealtimePublic{
			Enabled:   s.config.RealtimeEnabled(),
			Provider:  s.config.RealtimeProvider(),
			Model:     s.config.RealtimeModel(),
			Voice:     s.config.RealtimeVoice(),
			Reasoning: s.config.RealtimeReasoning(),
			BaseURL:   s.config.RealtimeBaseURL(),
			HasAPIKey: s.config.Realtime != nil && s.config.Realtime.APIKey != "",
		},
	}
}

// VerifyAdminPassword returns nil when password matches the stored bcrypt hash.
// Returns an error when no password is set, when the hash is malformed, or when
// the password is wrong. Callers must not surface the specific error to clients
// (uniform "invalid credentials" message) to avoid leaking which case fired.
func (s *Service) VerifyAdminPassword(password string) error {
	if s.config.AdminPasswordHash == "" {
		return fmt.Errorf("admin password not configured")
	}
	return bcrypt.CompareHashAndPassword([]byte(s.config.AdminPasswordHash), []byte(password))
}

// UpdateConfig saves updated config fields. All fields are optional; empty strings are skipped.
// Side effects per field cluster: wifi → connect-wifi (wpa_supplicant reload),
// llm_model/thinking → openclaw, stt_language → openclaw NewSession + hal,
// voice-pipeline fields → hal. Other fields persist only; restart os-server for full effect.
func (s *Service) UpdateConfig(data domain.UpdateConfigRequest) error {
	// bcrypt is CPU-intensive; compute before acquiring the config lock.
	var adminHash string
	if data.AdminPassword != "" {
		hash, err := bcrypt.GenerateFromPassword([]byte(data.AdminPassword), bcrypt.DefaultCost)
		if err != nil {
			return fmt.Errorf("hash admin password: %w", err)
		}
		adminHash = string(hash)
	}

	// Validate the realtime payload before the lock so an invalid request fails
	// without a partial save (same as bcrypt above).
	if data.Realtime != nil {
		if err := s.validateRealtimeSet(*data.Realtime); err != nil {
			return err
		}
	}

	// All field mutations happen inside WithLockSave so they are marshalled
	// atomically — the watcher goroutine's SetLLMModel cannot interleave with
	// a partial config snapshot. Side-effect flags are captured inside the
	// closure so callers outside always see the post-save values.
	var (
		modelChanged    bool
		thinkingChanged bool
		baseURLChanged  bool
		wifiChanged     bool
		langChanged     bool
		voiceChanged    bool
		realtimeChanged bool
		channelChanged  bool
		newModel        string
		newSSID         string
		newPassword     string
		prevLang        string
		newLang         string
		chanReq         domain.AddChannelRequest
	)
	if err := s.config.WithLockSave(func(c *config.Config) {
		prevModel := c.LLMModel
		prevLang = c.STTLanguage
		// Snapshot voice-pipeline fields hal reads at boot (hal/server.py
		// :317-388 + hal/config.py:103-104). Used to gate hal restart
		// so wifi/channel/MQTT/admin-only saves don't bounce TTS.
		prevLLMAPIKey := c.LLMAPIKey
		prevLLMBaseURL := c.LLMBaseURL
		prevDeepgramAPIKey := c.DeepgramAPIKey
		prevSTTAPIKey := c.STTAPIKey
		prevTTSAPIKey := c.TTSAPIKey
		prevSTTBaseURL := c.STTBaseURL
		prevTTSBaseURL := c.TTSBaseURL
		prevTTSProvider := c.TTSProvider
		prevTTSVoice := c.TTSVoice
		// Snapshot channel fields so we can tell whether the messaging channel /
		// its tokens actually changed and need re-pushing into the gateway.
		prevChannel := c.Channel
		prevTelegramBotToken := c.TelegramBotToken
		prevTelegramUserID := c.TelegramUserID
		prevSlackBotToken := c.SlackBotToken
		prevSlackAppToken := c.SlackAppToken
		prevSlackUserID := c.SlackUserID
		prevDiscordBotToken := c.DiscordBotToken
		prevDiscordGuildID := c.DiscordGuildID
		prevDiscordUserID := c.DiscordUserID

		if data.LLMAPIKey != "" {
			c.LLMAPIKey = data.LLMAPIKey
		}
		if data.LLMBaseURL != "" {
			c.LLMBaseURL = normalizeBaseURL(data.LLMBaseURL)
		}
		if data.LLMModel != "" {
			c.LLMModel = data.LLMModel
		}
		modelChanged = data.LLMModel != "" && data.LLMModel != prevModel
		baseURLChanged = data.LLMBaseURL != "" && c.LLMBaseURL != prevLLMBaseURL
		newModel = c.LLMModel

		thinkingChanged = data.LLMDisableThinking != nil
		if thinkingChanged {
			c.LLMDisableThinking = data.LLMDisableThinking
		}

		// PATCH semantics: empty = leave existing value alone. Stops the
		// Settings page (which ships its full form body even when the operator
		// only edited one tab) from wiping STT/TTS/Deepgram fields it never showed.
		if data.DeepgramAPIKey != "" {
			c.DeepgramAPIKey = data.DeepgramAPIKey
		}
		if data.STTAPIKey != "" {
			c.STTAPIKey = data.STTAPIKey
		}
		if data.TTSAPIKey != "" {
			c.TTSAPIKey = data.TTSAPIKey
		}
		if data.STTBaseURL != "" {
			c.STTBaseURL = normalizeBaseURL(data.STTBaseURL)
		}
		if data.TTSBaseURL != "" {
			c.TTSBaseURL = normalizeBaseURL(data.TTSBaseURL)
		}
		// Operators pick a language; the matching Deepgram SKU is auto-derived
		// because end users don't know which model handles which language.
		if data.STTLanguage != "" {
			c.STTLanguage = data.STTLanguage
			c.STTModel = sttModelForLanguage(data.STTLanguage)
		}
		newLang = c.STTLanguage
		langChanged = prevLang != newLang

		if data.TTSProvider != "" {
			c.TTSProvider = data.TTSProvider
		}
		if data.TTSVoice != "" {
			c.TTSVoice = data.TTSVoice
		}
		// Realtime block (validated above the lock). Sent = apply + restart hal.
		if data.Realtime != nil {
			applyRealtimeSet(c, *data.Realtime)
			realtimeChanged = true
		}
		if data.DeviceID != "" {
			c.DeviceID = data.DeviceID
		}
		wifiChanged = data.SSID != "" && data.SSID != c.NetworkSSID
		if data.SSID != "" {
			c.NetworkSSID = data.SSID
		}
		if data.Password != "" {
			c.NetworkPassword = data.Password
		}
		// Capture for the WiFi goroutine (avoid reading config after lock release).
		newSSID = c.NetworkSSID
		newPassword = c.NetworkPassword

		if data.Channel != "" {
			c.Channel = data.Channel
		}
		switch c.Channel {
		case domain.ChannelSlack:
			if data.SlackBotToken != "" {
				c.SlackBotToken = data.SlackBotToken
			}
			if data.SlackAppToken != "" {
				c.SlackAppToken = data.SlackAppToken
			}
			if data.SlackUserID != "" {
				c.SlackUserID = data.SlackUserID
			}
		case domain.ChannelDiscord:
			if data.DiscordBotToken != "" {
				c.DiscordBotToken = data.DiscordBotToken
			}
			if data.DiscordGuildID != "" {
				c.DiscordGuildID = data.DiscordGuildID
			}
			if data.DiscordUserID != "" {
				c.DiscordUserID = data.DiscordUserID
			}
		case domain.ChannelWhatsapp:
			if data.WhatsappUserID != "" {
				c.WhatsappUserID = data.WhatsappUserID
			}
		default:
			if data.TelegramBotToken != "" {
				c.TelegramBotToken = data.TelegramBotToken
			}
			if data.TelegramUserID != "" {
				c.TelegramUserID = data.TelegramUserID
			}
		}
		if data.MQTTEndpoint != "" {
			c.MQTTEndpoint = data.MQTTEndpoint
		}
		if data.MQTTUsername != "" {
			c.MQTTUsername = data.MQTTUsername
		}
		if data.MQTTPassword != "" {
			c.MQTTPassword = data.MQTTPassword
		}
		if data.MQTTPort != 0 {
			c.MQTTPort = data.MQTTPort
		}
		if data.FAChannel != "" {
			c.FAChannel = data.FAChannel
		}
		if data.FDChannel != "" {
			c.FDChannel = data.FDChannel
		}
		// Admin password rotation. Empty = keep existing hash; non-empty = bcrypt
		// + replace. Existing sessions stay valid (signed by SessionSecret), so
		// rotating the password alone won't lock the active operator out.
		if adminHash != "" {
			c.AdminPasswordHash = adminHash
		}

		voiceChanged = c.LLMAPIKey != prevLLMAPIKey ||
			c.LLMBaseURL != prevLLMBaseURL ||
			c.DeepgramAPIKey != prevDeepgramAPIKey ||
			c.STTAPIKey != prevSTTAPIKey ||
			c.TTSAPIKey != prevTTSAPIKey ||
			c.STTBaseURL != prevSTTBaseURL ||
			c.TTSBaseURL != prevTTSBaseURL ||
			c.TTSProvider != prevTTSProvider ||
			c.TTSVoice != prevTTSVoice

		channelChanged = c.Channel != prevChannel ||
			c.TelegramBotToken != prevTelegramBotToken || c.TelegramUserID != prevTelegramUserID ||
			c.SlackBotToken != prevSlackBotToken || c.SlackAppToken != prevSlackAppToken || c.SlackUserID != prevSlackUserID ||
			c.DiscordBotToken != prevDiscordBotToken || c.DiscordGuildID != prevDiscordGuildID || c.DiscordUserID != prevDiscordUserID
		// Build the request from the post-save config (full current values, since
		// PATCH semantics mean a token the operator didn't touch keeps its value).
		chanReq = domain.AddChannelRequest{
			Channel:          c.Channel,
			TelegramBotToken: c.TelegramBotToken, TelegramUserID: c.TelegramUserID,
			SlackBotToken: c.SlackBotToken, SlackAppToken: c.SlackAppToken, SlackUserID: c.SlackUserID,
			DiscordBotToken: c.DiscordBotToken, DiscordGuildID: c.DiscordGuildID, DiscordUserID: c.DiscordUserID,
		}
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("config updated", "component", "device")
	if wifiChanged {
		go func() {
			slog.Info("reconnecting to new WiFi", "component", "device", "ssid", newSSID)
			if _, err := s.networkService.SetupNetwork(newSSID, newPassword); err != nil {
				slog.Error("WiFi reconnect failed", "component", "device", "error", err)
			}
		}()
	}
	// Channel/token edits via the Settings form only land in config.json, but the
	// gateway keeps messaging tokens in its OWN config (written by AddChannel:
	// `openclaw channels add` + plugin enable) and reads from there first. So a
	// save — even a gateway restart — won't apply them; re-run AddChannel to push
	// the change into the gateway. WhatsApp needs interactive QR pairing (MQTT
	// add_channel only), so it is excluded here.
	if channelChanged && chanReq.Channel != domain.ChannelWhatsapp {
		go func() {
			if _, err := s.AddChannel(context.Background(), chanReq); err != nil {
				slog.Error("apply channel change to gateway failed", "component", "device", "channel", chanReq.Channel, "error", err)
			} else {
				slog.Info("channel change applied to gateway", "component", "device", "channel", chanReq.Channel)
			}
		}()
	}
	// Sync primary model into openclaw.json (os-server → OpenClaw direction).
	// config.mu is released by WithLockSave above; openclaw calls now acquire
	// primarySyncMu without risk of deadlock (consistent lock order).
	// When thinking also changed, RefreshModelsConfig handles primary update +
	// reasoning patch in a single write + restart — skip UpdatePrimaryModel to
	// avoid a redundant gateway restart.
	if modelChanged && !thinkingChanged && !baseURLChanged && s.agentGateway != nil {
		if err := s.agentGateway.UpdatePrimaryModel(newModel); err != nil {
			slog.Warn("update openclaw primary model failed", "component", "device", "error", err)
		}
	}
	if (thinkingChanged || baseURLChanged) && s.agentGateway != nil {
		// RefreshModelsConfig syncs agents.defaults.model.primary, per-model
		// reasoning, and providers.autonomous.baseUrl in one write + restart.
		if err := s.agentGateway.RefreshModelsConfig(); err != nil {
			slog.Error("refresh models config failed", "component", "device", "error", err)
		}
	}
	// When the operator switches stt_language explicitly, drop the in-session
	// chat history so the LLM doesn't keep replying in the previous language
	// out of inertia. SOUL.md tells it the latest turn wins, but a heavily
	// English/Vietnamese-biased history can still pull the next reply back —
	// a fresh session is the cleanest break.
	if langChanged && s.agentGateway != nil {
		if key := s.agentGateway.GetSessionKey(); key != "" {
			go func() {
				if err := s.agentGateway.NewSession(key); err != nil {
					slog.Warn("openclaw NewSession on stt_language change failed", "component", "device", "error", err)
				} else {
					slog.Info("openclaw session reset for stt_language change", "component", "device", "from", prevLang, "to", newLang)
				}
			}()
		}
	}
	// Restart hal only when a field it reads at boot actually changed.
	// stt_language is covered by langChanged (hal reads it via stt_language /
	// derived stt_model). Wifi/channel/MQTT/admin saves skip the restart.
	if voiceChanged || langChanged || realtimeChanged {
		s.RePushVoiceConfig()
	}
	return nil
}

// UpdateVoiceConfig updates only TTS provider/voice and STT language — safe to call from MQTT
// handlers since it does not touch API keys, MQTT credentials, or WiFi config.
func (s *Service) UpdateVoiceConfig(provider, voice, language string) error {
	prevLang := s.config.STTLanguage
	if provider != "" {
		s.config.TTSProvider = provider
	}
	if voice != "" {
		s.config.TTSVoice = voice
	}
	if language != "" {
		s.config.STTLanguage = language
		s.config.STTModel = sttModelForLanguage(language)
	}
	if err := s.config.Save(); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("voice config updated", "component", "device", "provider", s.config.TTSProvider, "voice", s.config.TTSVoice, "language", s.config.STTLanguage)
	if language != "" && prevLang != s.config.STTLanguage && s.agentGateway != nil {
		if key := s.agentGateway.GetSessionKey(); key != "" {
			go func() {
				if err := s.agentGateway.NewSession(key); err != nil {
					slog.Warn("NewSession on language change failed", "component", "device", "error", err)
				}
			}()
		}
	}
	s.RePushVoiceConfig()
	return nil
}

// RePushVoiceConfig restarts hal so it picks up new TTS config from config.json.
func (s *Service) RePushVoiceConfig() {
	go func() {
		slog.Info("restarting hal for TTS config change", "component", "device", "voice", s.config.TTSVoice, "provider", s.config.TTSProvider)
		out, err := exec.Command("systemctl", "restart", "hal").CombinedOutput()
		if err != nil {
			slog.Warn("hal restart failed", "component", "device", "error", err, "output", string(out))
		} else {
			slog.Info("hal restarted for TTS config", "component", "device", "voice", s.config.TTSVoice, "provider", s.config.TTSProvider)
		}
	}()
}

// validateRealtimeSet checks a realtime payload before any write: the provider
// selector, and (when per-provider knobs are present) the target provider's
// voice/reasoning. The target is the provider being set, or the current one when
// `provider` is omitted. Returns a descriptive error; nothing is written.
func (s *Service) validateRealtimeSet(d domain.RealtimeSetData) error {
	if err := config.ValidateRealtimeProvider(d.Provider); err != nil {
		return err
	}
	if d.Model != "" || d.Voice != "" || d.Reasoning != "" {
		target := strings.TrimSpace(d.Provider)
		if target == "" {
			target = s.config.RealtimeProvider() // current resolved provider
		}
		if err := config.ValidateRealtimeKnobs(target, d.Voice, d.Reasoning); err != nil {
			return err
		}
	}
	return nil
}

// applyRealtimeSet mutates the `realtime` block in c per the payload. Caller must
// have run validateRealtimeSet first; this only writes. Empty/omitted fields leave
// the current value unchanged; per-provider knobs land in the active provider's
// sub-object. Must run inside WithLockSave.
func applyRealtimeSet(c *config.Config, d domain.RealtimeSetData) {
	if c.Realtime == nil {
		c.Realtime = config.DefaultRealtimeConfig()
	}
	rt := c.Realtime
	if d.Enabled != nil {
		rt.Enabled = d.Enabled
	}
	if d.Provider != "" {
		rt.Provider = strings.ToLower(strings.TrimSpace(d.Provider))
	}
	if d.APIKey != "" {
		rt.APIKey = d.APIKey
	}
	if d.BaseURL != "" {
		rt.BaseURL = d.BaseURL
	}
	if d.Model == "" && d.Voice == "" && d.Reasoning == "" {
		return
	}
	switch strings.ToLower(strings.TrimSpace(rt.Provider)) {
	case "gemini":
		if rt.Gemini == nil {
			rt.Gemini = &config.GeminiRealtime{}
		}
		if d.Model != "" {
			rt.Gemini.Model = d.Model
		}
		if d.Voice != "" {
			rt.Gemini.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.Gemini.ThinkingLevel = d.Reasoning
		}
	case "openai":
		if rt.OpenAI == nil {
			rt.OpenAI = &config.OpenAIRealtime{}
		}
		if d.Model != "" {
			rt.OpenAI.Model = d.Model
		}
		if d.Voice != "" {
			rt.OpenAI.Voice = d.Voice
		}
		if d.Reasoning != "" {
			rt.OpenAI.ReasoningEffort = d.Reasoning
		}
	}
}

// UpdateRealtimeConfig applies a realtime payload (MQTT realtime.set or the HTTP
// `realtime` field) to config.json under the config lock, then restarts hal so it
// reads the new block (HAL reads config.json at import).
func (s *Service) UpdateRealtimeConfig(d domain.RealtimeSetData) error {
	if err := s.validateRealtimeSet(d); err != nil {
		return err
	}
	if err := s.config.WithLockSave(func(c *config.Config) {
		applyRealtimeSet(c, d)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("realtime config updated", "component", "device",
		"provider", s.config.RealtimeProvider(), "enabled", s.config.RealtimeEnabled())
	s.RePushRealtimeConfig()
	return nil
}

// RePushRealtimeConfig restarts hal so it picks up the new realtime block from
// config.json (HAL reads it at import).
func (s *Service) RePushRealtimeConfig() {
	go func() {
		slog.Info("restarting hal for realtime config change", "component", "device", "provider", s.config.RealtimeProvider())
		out, err := exec.Command("systemctl", "restart", "hal").CombinedOutput()
		if err != nil {
			slog.Warn("hal restart failed", "component", "device", "error", err, "output", string(out))
		} else {
			slog.Info("hal restarted for realtime config", "component", "device", "provider", s.config.RealtimeProvider())
		}
	}()
}

// CurrentAgentRuntime returns the effective agentic backend, resolved the same
// way as internal/agent/factory.go: config.agent_runtime, else the device's
// DEVICE.md gateway.default, else openclaw. Used by GET /api/device/agent-runtime
// so the web settings page shows what is actually running.
func (s *Service) CurrentAgentRuntime() string {
	return CurrentAgentRuntimeFromConfig(s.config)
}

// CurrentAgentRuntimeFromConfig resolves the effective agentic backend without a
// Service receiver, so callers holding only a *config.Config (e.g. the MQTT info
// handler) can report what is actually running. Same precedence as factory.go:
// config.agent_runtime, else DEVICE.md gateway.default, else openclaw.
func CurrentAgentRuntimeFromConfig(cfg *config.Config) string {
	if r := strings.ToLower(strings.TrimSpace(cfg.AgentRuntime)); r != "" {
		return r
	}
	if g := GatewayDefault(cfg.DeviceTypeOrDefault()); g != "" {
		return strings.ToLower(strings.TrimSpace(g))
	}
	return domain.AgentRuntimeOpenClaw
}

// UpdateAgentRuntime swaps the agentic backend (openclaw / hermes / picoclaw). It
// runs switch-runtime and BLOCKS until the switch finishes, then persists
// config.agent_runtime ONLY if it landed — so a failed install never leaves disk
// pointing at a backend that isn't actually running. Shared by the MQTT
// hermes.setup / picoclaw.setup handlers and the HTTP config API.
//
// Returns:
//   - (true, nil)  — the switch landed; the caller MUST report success and then
//     call RestartForAgentRuntime so factory.go re-resolves the gateway.
//   - (false, nil) — no-op (already on the target); no restart needed.
//   - (false, err) — the switch failed and was rolled back to `old`.
//
// It deliberately does NOT restart os-server: the restart kills os-server, so it
// has to happen AFTER the caller has put its success ack on the wire. switch-runtime
// no longer restarts os-server either (it used to) — that move is what lets the
// caller's goroutine survive long enough to ack the real result.
func (s *Service) UpdateAgentRuntime(d domain.AgentRuntimeSetData) (bool, error) {
	runtime := strings.ToLower(strings.TrimSpace(d.Runtime))
	// Reject unknown values outright. factory.go falls back to openclaw on
	// garbage, but an unknown runtime from the BFF/web is a contract error we
	// surface rather than silently coerce.
	if !domain.IsValidAgentRuntime(runtime) {
		return false, fmt.Errorf("invalid runtime %q (want %s)", d.Runtime, strings.Join(domain.AgentRuntimes, "|"))
	}

	// Resolve the currently-active runtime BEFORE the save so switch-runtime
	// knows which backend to stop. Default to openclaw (matches factory.go).
	old := strings.ToLower(strings.TrimSpace(s.config.AgentRuntime))
	if old == "" {
		old = domain.AgentRuntimeOpenClaw
	}

	// No-op guard: re-sending the active runtime shouldn't churn services or
	// bounce os-server. Returns switched=false so the caller skips the restart.
	if old == runtime {
		slog.Info("agent runtime unchanged, skipping switch", "component", "device", "runtime", runtime)
		return false, nil
	}

	// Make sure the embedded switcher is on disk before we depend on it, and
	// materialize the target's embedded installer (if compiled in) so the switch
	// works fully offline — switch-runtime runs the local copy, no CDN needed.
	if err := ensureSwitchRuntime(); err != nil {
		return false, fmt.Errorf("install switch-runtime: %w", err)
	}
	if err := materializeInstaller(runtime); err != nil {
		return false, fmt.Errorf("materialize %s installer: %w", runtime, err)
	}
	// Refresh the pre-start hook on disk too, so a plain os-server OTA delivers
	// its latest version (config self-heal) even when the backend is already
	// installed and install.sh is therefore skipped. switch-runtime runs it
	// right before the backend starts. Non-fatal: a backend without a presync, or
	// a transient write error, must not block the switch.
	if err := materializePresync(runtime); err != nil {
		slog.Warn("materialize presync hook failed (non-fatal)", "component", "device", "runtime", runtime, "error", err)
	}

	slog.Info("running switch-runtime", "component", "device", "from", old, "to", runtime)

	// Run the switcher and WAIT for its exit code. We deliberately do NOT persist
	// config.agent_runtime first: if the install/start fails, config.json stays at
	// `old`, so the device — including after a crash or reboot mid-switch — resolves
	// the still-installed old backend instead of a half-installed new one. On failure
	// switch-runtime has already rolled the systemd units back to `old`, and since we
	// never touched config (memory or disk) there is nothing to revert.
	if err := s.runSwitchRuntime(runtime, old); err != nil {
		return false, fmt.Errorf("switch to %s failed, rolled back to %s: %w", runtime, old, err)
	}

	// Switch landed (NEW up, OLD stopped) — only NOW persist the new runtime, so the
	// imminent RestartForAgentRuntime (and every future boot) resolves it. If this
	// save fails the units are already on NEW while disk still says `old`; surface the
	// error so the caller skips the restart and an operator can re-trigger.
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.AgentRuntime = runtime
	}); err != nil {
		return false, fmt.Errorf("switch to %s landed but persisting agent_runtime failed: %w", runtime, err)
	}

	slog.Info("agent runtime switch landed", "component", "device", "from", old, "to", runtime)
	return true, nil
}

// runSwitchRuntime runs the embedded switcher in a transient systemd unit and
// blocks for its result. --wait propagates switch-runtime's exit code (so we learn
// landed-vs-rolled-back), --collect GCs the unit afterwards. Pass <new> <old> so
// the switch stays fully generic (no hardcoded backend list anywhere). Safe to wait
// on from this process because switch-runtime no longer restarts os-server.
func (s *Service) runSwitchRuntime(newRuntime, oldRuntime string) error {
	if err := exec.Command("systemd-run", "--quiet", "--collect", "--wait",
		"--unit=os-runtime-switch", switchRuntimeBin, newRuntime, oldRuntime).Run(); err != nil {
		return fmt.Errorf("switch-runtime exited non-zero (see `journalctl -u os-runtime-switch`): %w", err)
	}
	return nil
}

// RestartForAgentRuntime restarts os-server so internal/agent/factory.go re-resolves
// the gateway against the freshly-persisted runtime. The restart runs in its OWN
// transient systemd unit (systemd-run) — NOT inline: `systemctl restart os-server`
// tears down os-server's cgroup, which would SIGTERM a plain child mid-call
// ("signal: terminated") and is racy. systemd-run launches it in a separate cgroup
// that survives the teardown, then returns once the unit is started (before the
// teardown begins), so this returns cleanly and real launch failures still surface.
//
// The unit is named (--unit) and NOT --quiet on purpose: this is the one action that
// bounces the whole server, so we want systemd-run to log which unit ran it and to
// be able to `journalctl -u os-server-runtime-restart` after the fact. --collect GCs
// the unit once it's done. Callers MUST have already published their success ack —
// this kills os-server.
func (s *Service) RestartForAgentRuntime() error {
	if err := exec.Command("systemd-run", "--collect", "--unit=os-server-runtime-restart",
		"systemctl", "restart", "os-server").Run(); err != nil {
		return fmt.Errorf("spawn os-server restart: %w", err)
	}
	return nil
}

// sttModelForLanguage maps a BCP-47 language code to the Deepgram SKU exposed
// by the Autonomous STT proxy. Empty input → empty model so hal falls back
// to its built-in default (flux-general-en). Vietnamese rides on Nova-3 (added
// Jan 2026); Chinese still requires Nova-2 because Nova-3 hasn't shipped zh.
func sttModelForLanguage(lang string) string {
	switch lang {
	case "":
		return ""
	case i18n.LangEN:
		return "flux-general-en"
	case i18n.LangZh, i18n.LangZhCN, i18n.LangZhHans, i18n.LangZhTW, i18n.LangZhHant:
		return "nova-2-general"
	default:
		return "nova-3-general"
	}
}

// WaitForAgentReady polls agentGateway.IsReady until it returns true or the timeout elapses.
func (s *Service) WaitForAgentReady(timeout time.Duration) bool {
	if s.agentGateway == nil {
		return false
	}
	deadline := time.Now().Add(timeout)
	for {
		if s.agentGateway.IsReady() {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(500 * time.Millisecond)
	}
}
