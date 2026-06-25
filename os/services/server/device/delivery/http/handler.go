package http

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-playground/validator/v10"
	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/network"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/server/config"
	"go.autonomous.ai/os/server/serializers"
	"go.autonomous.ai/os/server/session"
)

// DeviceHandler represents the HTTP handler for device
type DeviceHandler struct {
	service        *device.Service
	networkService *network.Service
	config         *config.Config
}

func ProvideDeviceHandler(ds *device.Service, ns *network.Service, cfg *config.Config) DeviceHandler {
	return DeviceHandler{
		service:        ds,
		networkService: ns,
		config:         cfg,
	}
}

// Setup godoc
//
//	@Summary	setup device
//	@Schemes
//	@Description	setup device
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.SetupRequest		true	"setup request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/setup [post]
func (h *DeviceHandler) Setup(c *gin.Context) {
	var req domain.SetupRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	// Re-setup via `#force`: operator may omit secrets they already have on
	// file (the web form hides them when `has_*` reports configured). Merge
	// missing fields from the current config before validation so required
	// tags + ValidateChannel still pass when only the changed fields ship.
	if h.config.SetUpCompleted {
		mergeMissingFromConfig(&req, h.config)
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := req.ValidateChannel(); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	// If operator supplied an admin password, set the session cookie now so the
	// browser is logged in by the time it redirects post-setup. The hash itself
	// is persisted asynchronously inside service.Setup; the cookie validates
	// against SessionSecret (independent of the password hash), so there's no
	// race — any subsequent /api/* call sees a valid session immediately.
	if req.AdminPassword != "" {
		if err := session.Issue(c, h.config); err != nil {
			slog.Warn("setup: issue session failed", "component", "device", "error", err)
		}
	}

	go func() {
		time.Sleep(2 * time.Second)
		if err := h.service.Setup(req); err != nil {
			slog.Error("setup failed", "component", "device", "error", err)
			h.networkService.SwitchToAPMode()
			return
		}

		slog.Info("setup success", "component", "device")
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// GetConfig godoc
//
//	@Summary	get current device config (sanitized)
//	@Schemes
//	@Description	get current device config. Secrets (API keys, channel
//	@Description	tokens, passwords) are returned as Has* booleans only —
//	@Description	plaintext values never leave the device. Use PUT
//	@Description	/api/device/config to update individual secret fields.
//	@Tags			device
//	@Success		200	{object}	serializers.ResponseSuccess
//	@Router			/device/config [get]
func (h *DeviceHandler) GetConfig(c *gin.Context) {
	cfg := h.service.GetPublicConfig()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(cfg))
}

// SetupStatus godoc
//
//	@Summary	current setup phase + LAN IP
//	@Description	web polls this during the AP→STA transition to learn the
//	@Description	device's new LAN IP and redirect the user. Phase progresses
//	@Description	idle → connecting → connected (or failed).
//	@Tags			device
//	@Success		200	{object}	serializers.ResponseSuccess
//	@Router			/device/setup/status [get]
func (h *DeviceHandler) SetupStatus(c *gin.Context) {
	phase, lanIP, errMsg := h.service.SetupStatus()
	// `mac` (hardware-derived "<device_type>-XXXX") is exposed here intentionally — the
	// device already broadcasts `<device_type>-xxxx.local` via avahi-daemon on the LAN,
	// so the suffix isn't sensitive. The web client uses it to auto-redirect
	// 192.168.100.1 → <device_type>-xxxx.local even before the operator is authed,
	// since /api/device/config requires admin auth and fresh devices have
	// none.
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"phase":  phase,
		"lan_ip": lanIP,
		"error":  errMsg,
		"mac":    device.GetDeviceMac(),
	}))
}

// UpdateConfig godoc
//
//	@Summary	update device config
//	@Schemes
//	@Description	update device config fields (all optional; saves to disk, restart os-server for full effect)
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.UpdateConfigRequest	true	"update config request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/config [put]
func (h *DeviceHandler) UpdateConfig(c *gin.Context) {
	var req domain.UpdateConfigRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.UpdateConfig(req); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// GetVoices returns the list of available TTS voices for the requested provider.
// Tries HAL /voice/voices?provider=&lang= first (source of truth), falls
// back to a static list. `lang` (BCP-47 stt_language code) lets the web UI
// filter voices to those that sound natural in the active language; empty
// lang returns the full flat list.
func (h *DeviceHandler) GetVoices(c *gin.Context) {
	provider := c.DefaultQuery("provider", domain.TTSProviderOpenAI)
	lang := c.Query("lang")

	if voices, err := hal.ListVoices(provider, lang); err == nil && len(voices) > 0 {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
		return
	}
	// Fallback to static list (no language filtering — static list is EN-only)
	voices, ok := domain.TTSVoicesByProvider[provider]
	if !ok {
		voices = domain.TTSVoices
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
}

// GetTTSProviders returns the list of supported TTS providers.
func (h *DeviceHandler) GetTTSProviders(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.TTSProviders))
}

// GetRealtimeOptions returns the valid realtime providers + per-provider voice /
// reasoning lists, so the web never hardcodes them (single source = config).
func (h *DeviceHandler) GetRealtimeOptions(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(config.GetRealtimeOptions()))
}

// GetAgentRuntime returns the active agentic backend + selectable options for
// the web settings dropdown.
//
//	@Router	/device/agent-runtime [get]
func (h *DeviceHandler) GetAgentRuntime(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.AgentRuntimeStatus{
		Current: h.service.CurrentAgentRuntime(),
		Options: domain.AgentRuntimes,
	}))
}

// SetAgentRuntime swaps the agentic backend (openclaw / hermes / picoclaw). The
// switch now BLOCKS until it lands (UpdateAgentRuntime waits on switch-runtime,
// which may install the backend), so we validate the runtime synchronously for the
// 400 and run the switch in the background, returning 200 "accepted" right away.
// On a successful switch os-server restarts itself, so the HTTP connection drops
// shortly after — the web should treat 200 as "accepted, reconnecting" and re-poll
// GetAgentRuntime / the agent banner once os-server is back.
//
//	@Router	/device/agent-runtime [post]
func (h *DeviceHandler) SetAgentRuntime(c *gin.Context) {
	var req domain.AgentRuntimeSetData
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if !domain.IsValidAgentRuntime(req.Runtime) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(
			fmt.Sprintf("invalid runtime %q (want %s)", req.Runtime, strings.Join(domain.AgentRuntimes, "|"))))
		return
	}
	go func() {
		switched, err := h.service.UpdateAgentRuntime(req)
		if err != nil {
			slog.Error("agent-runtime switch failed", "component", "device-http", "error", err)
			return
		}
		if switched {
			if rerr := h.service.RestartForAgentRuntime(); rerr != nil {
				slog.Error("agent-runtime os-server restart failed", "component", "device-http", "error", rerr)
			}
		}
	}()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// ChangeChannel godoc
//
//	@Summary	change messaging channel
//	@Schemes
//	@Description	change messaging channel (telegram/slack/discord) without full device re-setup
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.ChangeChannelRequest	true	"change channel request"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/channel [post]
func (h *DeviceHandler) ChangeChannel(c *gin.Context) {
	var req domain.AddChannelRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := req.ValidateChannel(); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	// WhatsApp pairing streams a QR back to the caller; HTTP's fire-and-forget
	// shape can't deliver that. Force the canonical MQTT add_channel path.
	if req.EffectiveChannel() == domain.ChannelWhatsapp {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("whatsapp pairing not supported via HTTP; use MQTT add_channel"))
		return
	}
	// Reject a channel the active runtime can't run synchronously — the
	// fire-and-forget goroutine below couldn't surface the not-supported error.
	if !h.service.SupportsChannel(req.EffectiveChannel()) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(req.EffectiveChannel()+" not supported on the active runtime"))
		return
	}

	go func() {
		// Background context — HTTP request is fire-and-forget; subprocess
		// invocations inside AddChannel take ~seconds, not minutes, for
		// telegram/slack/discord.
		if _, err := h.service.AddChannel(context.Background(), req); err != nil {
			slog.Error("add channel failed", "component", "device", "error", err)
			return
		}
		slog.Info("add channel success", "component", "device")
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// mergeMissingFromConfig fills empty SetupRequest fields with the values
// already saved in config.json. Re-setup callers (web `#force`, scripts)
// can omit any secret/identifier they don't intend to change — the
// previously-saved value rides through into validation + the Setup pipeline
// unchanged. AdminPassword is left alone on purpose (operator either sets a
// new one or skips that field entirely).
func mergeMissingFromConfig(req *domain.SetupRequest, cfg *config.Config) {
	if req.SSID == "" {
		req.SSID = cfg.NetworkSSID
	}
	if req.Password == "" {
		req.Password = cfg.NetworkPassword
	}
	if req.LLMAPIKey == "" {
		req.LLMAPIKey = cfg.LLMAPIKey
	}
	if req.LLMBaseURL == "" {
		req.LLMBaseURL = cfg.LLMBaseURL
	}
	if req.LLMModel == "" {
		req.LLMModel = cfg.LLMModel
	}
	if req.DeviceID == "" {
		req.DeviceID = cfg.DeviceID
	}
	if req.Channel == "" {
		req.Channel = cfg.Channel
	}
	if req.TelegramBotToken == "" {
		req.TelegramBotToken = cfg.TelegramBotToken
	}
	if req.TelegramUserID == "" {
		req.TelegramUserID = cfg.TelegramUserID
	}
	if req.SlackBotToken == "" {
		req.SlackBotToken = cfg.SlackBotToken
	}
	if req.SlackAppToken == "" {
		req.SlackAppToken = cfg.SlackAppToken
	}
	if req.SlackUserID == "" {
		req.SlackUserID = cfg.SlackUserID
	}
	if req.DiscordBotToken == "" {
		req.DiscordBotToken = cfg.DiscordBotToken
	}
	if req.DiscordGuildID == "" {
		req.DiscordGuildID = cfg.DiscordGuildID
	}
	if req.DiscordUserID == "" {
		req.DiscordUserID = cfg.DiscordUserID
	}
	if req.DeepgramAPIKey == "" {
		req.DeepgramAPIKey = cfg.DeepgramAPIKey
	}
	if req.STTAPIKey == "" {
		req.STTAPIKey = cfg.STTAPIKey
	}
	if req.TTSAPIKey == "" {
		req.TTSAPIKey = cfg.TTSAPIKey
	}
	if req.STTBaseURL == "" {
		req.STTBaseURL = cfg.STTBaseURL
	}
	if req.TTSBaseURL == "" {
		req.TTSBaseURL = cfg.TTSBaseURL
	}
	if req.STTLanguage == "" {
		req.STTLanguage = cfg.STTLanguage
	}
	if req.TTSProvider == "" {
		req.TTSProvider = cfg.TTSProvider
	}
	if req.TTSVoice == "" {
		req.TTSVoice = cfg.TTSVoice
	}
	if req.MQTTEndpoint == "" {
		req.MQTTEndpoint = cfg.MQTTEndpoint
	}
	if req.MQTTUsername == "" {
		req.MQTTUsername = cfg.MQTTUsername
	}
	if req.MQTTPassword == "" {
		req.MQTTPassword = cfg.MQTTPassword
	}
	if req.MQTTPort == 0 {
		req.MQTTPort = cfg.MQTTPort
	}
	if req.FAChannel == "" {
		req.FAChannel = cfg.FAChannel
	}
	if req.FDChannel == "" {
		req.FDChannel = cfg.FDChannel
	}
}
