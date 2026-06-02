package device

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/beclient"
	"go-lamp.autonomous.ai/internal/network"
	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/server/config"
)

// Setup phase strings exposed via /api/setup/status so the web client can
// follow the device through the AP→STA transition. Phases progress only
// forward; failures park at "failed".
const (
	SetupPhaseIdle       = "idle"
	SetupPhaseConnecting = "connecting"
	SetupPhaseConnected  = "connected"
	SetupPhaseFailed     = "failed"
)

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
// client can still detect "you're at the AP IP but the lamp lives at X"
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
	s.setupState.set(SetupPhaseConnecting, "", "")
	result, err := s.networkService.SetupNetwork(data.SSID, data.Password)
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
	if ip, ipErr := s.networkService.GetCurrentIP(); ipErr == nil && ip != "" {
		s.setupState.set(SetupPhaseConnected, ip, "")
		slog.Info("setup: WiFi associated", "component", "device", "lan_ip", ip)
	} else {
		s.setupState.set(SetupPhaseConnected, "", "")
		slog.Warn("setup: WiFi associated but no IP detected", "component", "device", "error", ipErr)
	}

	if err := s.agentGateway.SetupAgent(data); err != nil {
		return err
	}

	llmAPIKey := data.LLMAPIKey
	llmModel := data.LLMModel
	llmBaseURL := data.LLMBaseURL
	channel := data.EffectiveChannel()

	s.config.LLMAPIKey = llmAPIKey
	s.config.LLMBaseURL = llmBaseURL
	s.config.LLMModel = llmModel
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
			Version:        config.LampVersion,
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
				Version:        config.LampVersion,
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
// llm_model/thinking → openclaw, stt_language → openclaw NewSession + lelamp,
// voice-pipeline fields → lelamp. Other fields persist only; restart Lamp for full effect.
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

	// All field mutations happen inside WithLockSave so they are marshalled
	// atomically — the watcher goroutine's SetLLMModel cannot interleave with
	// a partial config snapshot. Side-effect flags are captured inside the
	// closure so callers outside always see the post-save values.
	var (
		modelChanged    bool
		thinkingChanged bool
		wifiChanged     bool
		langChanged     bool
		voiceChanged    bool
		newModel        string
		newSSID         string
		newPassword     string
		prevLang        string
		newLang         string
	)
	if err := s.config.WithLockSave(func(c *config.Config) {
		prevModel := c.LLMModel
		prevLang = c.STTLanguage
		// Snapshot voice-pipeline fields lelamp reads at boot (lelamp/server.py
		// :317-388 + lelamp/config.py:103-104). Used to gate lamp-lelamp restart
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

		if data.LLMAPIKey != "" {
			c.LLMAPIKey = data.LLMAPIKey
		}
		if data.LLMBaseURL != "" {
			c.LLMBaseURL = data.LLMBaseURL
		}
		if data.LLMModel != "" {
			c.LLMModel = data.LLMModel
		}
		modelChanged = data.LLMModel != "" && data.LLMModel != prevModel
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
			c.STTBaseURL = data.STTBaseURL
		}
		if data.TTSBaseURL != "" {
			c.TTSBaseURL = data.TTSBaseURL
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
	// Sync primary model into openclaw.json (Lamp → OpenClaw direction).
	// config.mu is released by WithLockSave above; openclaw calls now acquire
	// primarySyncMu without risk of deadlock (consistent lock order).
	// When thinking also changed, RefreshModelsConfig handles primary update +
	// reasoning patch in a single write + restart — skip UpdatePrimaryModel to
	// avoid a redundant gateway restart.
	if modelChanged && !thinkingChanged && s.agentGateway != nil {
		if err := s.agentGateway.UpdatePrimaryModel(newModel); err != nil {
			slog.Warn("update openclaw primary model failed", "component", "device", "error", err)
		}
	}
	if thinkingChanged && s.agentGateway != nil {
		// RefreshModelsConfig also syncs agents.defaults.model.primary from
		// s.config.LLMModel, so one restart covers both model and thinking changes.
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
	// Restart lamp-lelamp only when a field it reads at boot actually changed.
	// stt_language is covered by langChanged (lelamp reads it via stt_language /
	// derived stt_model). Wifi/channel/MQTT/admin saves skip the restart.
	if voiceChanged || langChanged {
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

// RePushVoiceConfig restarts lamp-lelamp so it picks up new TTS config from config.json.
func (s *Service) RePushVoiceConfig() {
	go func() {
		slog.Info("restarting lamp-lelamp for TTS config change", "component", "device", "voice", s.config.TTSVoice, "provider", s.config.TTSProvider)
		out, err := exec.Command("systemctl", "restart", "lamp-lelamp").CombinedOutput()
		if err != nil {
			slog.Warn("lamp-lelamp restart failed", "component", "device", "error", err, "output", string(out))
		} else {
			slog.Info("lamp-lelamp restarted for TTS config", "component", "device", "voice", s.config.TTSVoice, "provider", s.config.TTSProvider)
		}
	}()
}

// sttModelForLanguage maps a BCP-47 language code to the Deepgram SKU exposed
// by the Autonomous STT proxy. Empty input → empty model so lelamp falls back
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
