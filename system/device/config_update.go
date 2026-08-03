package device

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/server/config"
)

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
		WakeWord:           s.config.WakeWordEnabled(),
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
			// Expose ONLY the explicit override (blank when deriving) so the web
			// form's "leave blank to derive" works and does not re-persist a bare
			// URL that breaks HAL's /ws/gemini handshake. See RealtimeBaseURL doc.
			BaseURL:   s.config.RealtimeBaseURLOverride(),
			HasAPIKey: s.config.RealtimeHasAPIKey(),
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

// updateChanges captures which field clusters an UpdateConfig save actually
// changed, plus the post-save values the side-effect blocks need. Computed
// entirely inside the WithLockSave closure (by applyUpdate) so the flags always
// describe the atomically-marshalled state, never a torn snapshot.
type updateChanges struct {
	model    bool // llm_model changed → sync primary into the gateway
	thinking bool // llm_disable_thinking sent → RefreshModelsConfig
	baseURL  bool // llm_base_url changed → RefreshModelsConfig
	wifi     bool // ssid changed → reconnect WiFi
	lang     bool // stt_language changed → new agent session + hal restart
	voice    bool // a field hal reads at boot changed → hal restart
	realtime bool // realtime block sent → hal restart
	channel  bool // messaging channel/tokens changed → re-push into gateway

	newModel    string
	newSSID     string
	newPassword string
	prevLang    string
	newLang     string
	chanReq     domain.AddChannelRequest
}

// voiceSnapshot is the set of config fields hal reads at boot (hal/server.py
// :317-388 + hal/config.py:103-104). Comparable, so a plain before/after
// equality check gates the hal restart — wifi/channel/MQTT/admin-only saves
// must not bounce TTS.
type voiceSnapshot struct {
	llmAPIKey      string
	llmBaseURL     string
	deepgramAPIKey string
	sttAPIKey      string
	ttsAPIKey      string
	sttBaseURL     string
	ttsBaseURL     string
	ttsProvider    string
	ttsVoice       string
}

func voiceFields(c *config.Config) voiceSnapshot {
	return voiceSnapshot{
		llmAPIKey:      c.LLMAPIKey,
		llmBaseURL:     c.LLMBaseURL,
		deepgramAPIKey: c.DeepgramAPIKey,
		sttAPIKey:      c.STTAPIKey,
		ttsAPIKey:      c.TTSAPIKey,
		sttBaseURL:     c.STTBaseURL,
		ttsBaseURL:     c.TTSBaseURL,
		ttsProvider:    c.TTSProvider,
		ttsVoice:       c.TTSVoice,
	}
}

// channelSnapshot is the messaging channel identity + tokens. Comparable; a
// before/after difference means the change only landed in config.json and must
// be re-pushed into the gateway (which keeps tokens in its OWN config).
type channelSnapshot struct {
	channel          string
	telegramBotToken string
	telegramUserID   string
	slackBotToken    string
	slackAppToken    string
	slackUserID      string
	discordBotToken  string
	discordGuildID   string
	discordUserID    string
}

func channelFields(c *config.Config) channelSnapshot {
	return channelSnapshot{
		channel:          c.Channel,
		telegramBotToken: c.TelegramBotToken,
		telegramUserID:   c.TelegramUserID,
		slackBotToken:    c.SlackBotToken,
		slackAppToken:    c.SlackAppToken,
		slackUserID:      c.SlackUserID,
		discordBotToken:  c.DiscordBotToken,
		discordGuildID:   c.DiscordGuildID,
		discordUserID:    c.DiscordUserID,
	}
}

// applyUpdate mutates c per the PATCH request and reports what changed. Pure
// config-in/config-out (no I/O, no locking) so the flag logic — the part of
// UpdateConfig where a mistake silently skips or duplicates a restart — is unit
// testable. Must run inside WithLockSave; adminHash is precomputed by the
// caller because bcrypt is too CPU-heavy for the lock.
func applyUpdate(c *config.Config, data domain.UpdateConfigRequest, adminHash string) updateChanges {
	var ch updateChanges
	prevVoice := voiceFields(c)
	prevWakeWord := c.WakeWordEnabled()
	prevChannel := channelFields(c)
	ch.prevLang = c.STTLanguage

	applyLLMFields(c, data, &ch)
	applyVoicePipelineFields(c, data, &ch)

	if data.DeviceID != "" {
		c.DeviceID = data.DeviceID
	}
	applyNetworkFields(c, data, &ch)
	applyChannelPatch(c, data)
	applyMQTTFields(c, data)

	// Admin password rotation. Empty = keep existing hash; non-empty = bcrypt
	// + replace. Existing sessions stay valid (signed by SessionSecret), so
	// rotating the password alone won't lock the active operator out.
	if adminHash != "" {
		c.AdminPasswordHash = adminHash
	}

	ch.voice = voiceFields(c) != prevVoice || c.WakeWordEnabled() != prevWakeWord
	ch.channel = channelFields(c) != prevChannel
	// Build the request from the post-save config (full current values, since
	// PATCH semantics mean a token the operator didn't touch keeps its value).
	ch.chanReq = domain.AddChannelRequest{
		Channel:          c.Channel,
		TelegramBotToken: c.TelegramBotToken, TelegramUserID: c.TelegramUserID,
		SlackBotToken: c.SlackBotToken, SlackAppToken: c.SlackAppToken, SlackUserID: c.SlackUserID,
		DiscordBotToken: c.DiscordBotToken, DiscordGuildID: c.DiscordGuildID, DiscordUserID: c.DiscordUserID,
	}
	return ch
}

func applyLLMFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	prevModel := c.LLMModel
	prevBaseURL := c.LLMBaseURL
	if data.LLMAPIKey != "" {
		c.LLMAPIKey = data.LLMAPIKey
	}
	if data.LLMBaseURL != "" {
		c.LLMBaseURL = urlnorm.NormalizeBaseURL(data.LLMBaseURL)
	}
	if data.LLMModel != "" {
		c.LLMModel = data.LLMModel
	}
	ch.model = data.LLMModel != "" && data.LLMModel != prevModel
	ch.baseURL = data.LLMBaseURL != "" && c.LLMBaseURL != prevBaseURL
	ch.newModel = c.LLMModel

	ch.thinking = data.LLMDisableThinking != nil
	if ch.thinking {
		c.LLMDisableThinking = data.LLMDisableThinking
	}
}

// applyVoicePipelineFields applies the STT/TTS/realtime cluster. PATCH
// semantics: empty = leave existing value alone. Stops the Settings page
// (which ships its full form body even when the operator only edited one tab)
// from wiping STT/TTS/Deepgram fields it never showed.
func applyVoicePipelineFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	if data.WakeWord != nil {
		c.WakeWord = data.WakeWord
	}
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
		c.STTBaseURL = urlnorm.NormalizeBaseURL(data.STTBaseURL)
	}
	if data.TTSBaseURL != "" {
		c.TTSBaseURL = urlnorm.NormalizeBaseURL(data.TTSBaseURL)
	}
	// Operators pick a language; the matching Deepgram SKU is auto-derived
	// because end users don't know which model handles which language.
	if data.STTLanguage != "" {
		c.STTLanguage = data.STTLanguage
		c.STTModel = sttModelForLanguage(data.STTLanguage)
	}
	ch.newLang = c.STTLanguage
	ch.lang = ch.prevLang != ch.newLang

	if data.TTSProvider != "" {
		c.TTSProvider = data.TTSProvider
	}
	if data.TTSVoice != "" {
		c.TTSVoice = data.TTSVoice
	}
	// Realtime block (validated by the caller before the lock). Sent = apply +
	// restart hal.
	if data.Realtime != nil {
		applyRealtimeSet(c, *data.Realtime)
		ch.realtime = true
	}
}

func applyNetworkFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	ch.wifi = data.SSID != "" && data.SSID != c.NetworkSSID
	if data.SSID != "" {
		c.NetworkSSID = data.SSID
	}
	if data.Password != "" {
		c.NetworkPassword = data.Password
	}
	// Capture for the WiFi goroutine (avoid reading config after lock release).
	ch.newSSID = c.NetworkSSID
	ch.newPassword = c.NetworkPassword
}

func applyChannelPatch(c *config.Config, data domain.UpdateConfigRequest) {
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
}

func applyMQTTFields(c *config.Config, data domain.UpdateConfigRequest) {
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
	// a partial config snapshot.
	var ch updateChanges
	if err := s.config.WithLockSave(func(c *config.Config) {
		ch = applyUpdate(c, data, adminHash)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("config updated", "component", "device")
	s.fireConfigSideEffects(ch)
	return nil
}

// fireConfigSideEffects runs the per-cluster follow-ups after a successful
// UpdateConfig save. config.mu is already released, so the gateway calls below
// acquire their own locks without deadlock risk (consistent lock order).
func (s *Service) fireConfigSideEffects(ch updateChanges) {
	if ch.wifi {
		go func() {
			slog.Info("reconnecting to new WiFi", "component", "device", "ssid", ch.newSSID)
			if _, err := s.networkService.SetupNetwork(ch.newSSID, ch.newPassword); err != nil {
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
	if ch.channel && ch.chanReq.Channel != domain.ChannelWhatsapp {
		go func() {
			if _, err := s.AddChannel(context.Background(), ch.chanReq); err != nil {
				slog.Error("apply channel change to gateway failed", "component", "device", "channel", ch.chanReq.Channel, "error", err)
			} else {
				slog.Info("channel change applied to gateway", "component", "device", "channel", ch.chanReq.Channel)
			}
		}()
	}
	s.syncLLMToGateway(ch)
	// When the operator switches stt_language explicitly, drop the in-session
	// chat history so the LLM doesn't keep replying in the previous language
	// out of inertia. SOUL.md tells it the latest turn wins, but a heavily
	// English/Vietnamese-biased history can still pull the next reply back —
	// a fresh session is the cleanest break.
	if ch.lang && s.agentGateway != nil {
		if key := s.agentGateway.GetSessionKey(); key != "" {
			go func() {
				if err := s.agentGateway.NewSession(key); err != nil {
					slog.Warn("openclaw NewSession on stt_language change failed", "component", "device", "error", err)
				} else {
					slog.Info("openclaw session reset for stt_language change", "component", "device", "from", ch.prevLang, "to", ch.newLang)
				}
			}()
		}
	}
	// Restart hal only when a field it reads at boot actually changed.
	// stt_language is covered by ch.lang (hal reads it via stt_language /
	// derived stt_model). Wifi/channel/MQTT/admin saves skip the restart.
	if ch.voice || ch.lang || ch.realtime {
		s.restartHAL("voice config change")
	}
}

// syncLLMToGateway pushes a model/thinking/baseURL change into the active
// gateway's own config. Sync primary model is the os-server → OpenClaw
// direction. When thinking or baseURL also changed, RefreshModelsConfig
// handles primary update + reasoning patch + baseUrl in a single write +
// restart — skip UpdatePrimaryModel to avoid a redundant gateway restart.
func (s *Service) syncLLMToGateway(ch updateChanges) {
	if s.agentGateway == nil {
		return
	}
	if ch.model && !ch.thinking && !ch.baseURL {
		if err := s.agentGateway.UpdatePrimaryModel(ch.newModel); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// hermes/picoclaw: the device model is not what the runtime runs
				// on, so there is nothing to apply — informational, not a failure.
				slog.Info("primary model sync not supported by runtime", "component", "device", "backend", s.agentGateway.Name())
			} else {
				slog.Warn("update primary model failed", "component", "device", "error", err)
			}
		}
	}
	if ch.thinking || ch.baseURL {
		// RefreshModelsConfig syncs agents.defaults.model.primary, per-model
		// reasoning, and providers.autonomous.baseUrl in one write + restart.
		if err := s.agentGateway.RefreshModelsConfig(); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// hermes/picoclaw can't be patched directly, but hermes's
				// EnsureOnboarding presync re-reads llm_base_url/llm_api_key from
				// the config.json we just saved — run it so a baseURL/key change
				// applies now instead of at the next boot. Idempotent on both
				// backends; restarts the gateway only when the config changed.
				slog.Info("models config not device-patchable, running onboarding self-heal", "component", "device", "backend", s.agentGateway.Name())
				go func() {
					if err := s.agentGateway.EnsureOnboarding(); err != nil {
						slog.Warn("onboarding self-heal after llm config change failed", "component", "device", "error", err)
					}
				}()
			} else {
				slog.Error("refresh models config failed", "component", "device", "error", err)
			}
		}
	}
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
	s.restartHAL("voice config change")
	return nil
}

// sttModelForLanguage maps a BCP-47 language code to the Deepgram SKU exposed
// by the Autonomous STT proxy. Empty input → empty model so hal falls back
// to its built-in default (flux-general-en). English stays on Flux (Deepgram's
// conversational EN model — EN-only). Everything else rides on Nova-3:
// Vietnamese since Jan 2026, Mandarin (zh/zh-CN/zh-Hans/zh-TW/zh-Hant) since
// Deepgram's 2026-03-31 release — zh was pinned to Nova-2 before that.
func sttModelForLanguage(lang string) string {
	switch lang {
	case "":
		return ""
	case i18n.LangEN:
		return "flux-general-en"
	default:
		return "nova-3-general"
	}
}
