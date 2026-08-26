package http

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-playground/validator/v10"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
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
		slog.Warn("setup bind json failed", "component", "device", "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("setup request received", "component", "device",
		"ssid_len", len(req.SSID),
		"wifi_password_len", len(req.Password),
		"admin_password_len", len(req.AdminPassword),
		"llm_api_key_len", len(req.LLMAPIKey),
		"llm_base_url_len", len(req.LLMBaseURL),
		"device_id_len", len(req.DeviceID),
		"channel", req.Channel,
		"set_up_completed", h.config.SetUpCompleted,
		"admin_hash_on_file", h.config.AdminPasswordHash != "",
	)
	// First-time setup with no admin password supplied: default it to the
	// device's hardware suffix (last 4 chars after '-' in GetDeviceMac(), e.g.
	// "intern-993f" → "993f"). This suffix matches the AP hotspot SSID (set by
	// scripts/provision/setup-ap.sh with identical logic) and is printed on
	// the sticker at the bottom of the device, so operators can sign in without
	// picking a password during setup. Only fires when the device has no
	// admin_password_hash yet — re-setup on a provisioned device keeps its
	// existing hash. Fails 400 rather than silently falling back to a
	// hardcoded default when the mac is unreadable (no DEVICE_TYPE env, no
	// serial, no eth MAC) — silent fallback would give every unidentified
	// device the same well-known password.
	if req.AdminPassword == "" && !h.config.SetUpCompleted && h.config.AdminPasswordHash == "" {
		mac := device.GetDeviceMac()
		dash := strings.LastIndex(mac, "-")
		slog.Info("admin_password default: input snapshot", "component", "device",
			"mac", mac,
			"mac_len", len(mac),
			"last_dash_idx", dash,
		)
		if mac == "" || dash < 0 || dash == len(mac)-1 {
			slog.Warn("admin_password default failed — device id unreadable", "component", "device",
				"mac", mac, "reason", "empty mac or malformed dash position")
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"device hardware ID unreadable — cannot default admin_password (set it manually)"))
			return
		}
		req.AdminPassword = mac[dash+1:]
		slog.Info("admin_password defaulted to device suffix", "component", "device",
			"suffix", req.AdminPassword, "suffix_len", len(req.AdminPassword))
	} else {
		slog.Info("admin_password default skipped", "component", "device",
			"has_admin_password_in_req", req.AdminPassword != "",
			"set_up_completed", h.config.SetUpCompleted,
			"admin_hash_on_file", h.config.AdminPasswordHash != "",
		)
	}
	// Re-setup via `#force`: operator may omit secrets they already have on
	// file (the web form hides them when `has_*` reports configured). Merge
	// missing fields from the current config before validation so required
	// tags + ValidateChannel still pass when only the changed fields ship.
	//
	// Deliberately NOT gated on SetUpCompleted. A setup that fails at the Wi-Fi
	// step (wrong password) never reaches the config writes in device.Setup, so
	// the device stays SetUpCompleted=false — yet the operator's browser may
	// well have lost the pushed credentials by the time they retry (the AP
	// teardown kills the tab's sessionStorage, and a popup reopened without the
	// original query string comes back empty). Gating here meant that retry
	// failed validation on LLMAPIKey and surfaced "Missing: AI Brain API key" to
	// someone who had only mistyped their Wi-Fi password.
	//
	// Safe by construction: mergeMissingFromConfig only fills slots the request
	// left empty, and only from this device's own config, so it can neither
	// override what the operator sent nor introduce a value they couldn't
	// already read back. On a genuinely fresh device the config is empty, the
	// merge is a no-op, and validation still rejects an incomplete request.
	mergeMissingFromConfig(&req, h.config)
	if err := validator.New().Struct(req); err != nil {
		slog.Warn("setup validator failed", "component", "device", "error", err.Error(),
			"ssid_set", req.SSID != "", "password_set", req.Password != "",
			"llm_api_key_set", req.LLMAPIKey != "", "llm_base_url_set", req.LLMBaseURL != "",
			"device_id_set", req.DeviceID != "")
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	// Messaging channels are optional during initial setup. Keep the selected
	// channel's validation available for re-enabling this policy if needed;
	// POST /device/channel and MQTT add_channel still validate credentials.
	// if err := req.ValidateChannel(); err != nil {
	// 	slog.Warn("setup channel validation failed", "component", "device", "error", err.Error(), "channel", req.Channel)
	// 	c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
	// 	return
	// }

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

// WifiProvision godoc
//
//	@Summary	provision Wi-Fi only (AP-portal fast path)
//	@Description	Dedicated re-provisioning endpoint for a device that is already
//	@Description	fully configured but has been moved to a new Wi-Fi network.
//	@Description	Body is minimal ({ssid, password}); no LLM/channel/device_id
//	@Description	fields are touched. Runs the connect-wifi script + AP teardown.
//	@Description	Gated by apOnlyMiddleware (source IP must be in the AP subnet).
//	@Tags			device
//	@Accept			json
//	@Param			body	body		domain.WifiProvisionRequest	true	"wifi credentials"
//	@Success		200		{object}	serializers.ResponseSuccess
//	@Router			/device/wifi-provision [post]
func (h *DeviceHandler) WifiProvision(c *gin.Context) {
	var req domain.WifiProvisionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		slog.Warn("wifi-provision bind json failed", "component", "device", "error", err)
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		slog.Warn("wifi-provision validator failed", "component", "device", "error", err.Error())
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("wifi-provision request", "component", "device",
		"ssid_len", len(req.SSID),
		"password_len", len(req.Password),
		"llm_api_key_supplied", req.LLMAPIKey != "",
		"llm_api_key_len", len(req.LLMAPIKey),
		"llm_base_url_supplied", req.LLMBaseURL != "",
		"llm_base_url", req.LLMBaseURL, // safe to log — not a secret
		"llm_model", req.LLMModel,
		"admin_password_supplied", req.AdminPassword != "",
		"set_up_completed", h.config.SetUpCompleted,
	)

	// Fresh device (never set up) MUST supply an LLM triplet — otherwise the
	// device joins Wi-Fi but has no brain, and the operator ends up in the
	// admin page seeing "Auto-AI / campaign-api.autonomous.ai" defaults that
	// won't actually resolve to a working chat. Once the device is provisioned,
	// missing fields keep their on-disk values (mergeMissingFromConfig
	// semantics) — the operator changing Wi-Fi shouldn't have to retype the
	// API key.
	if !h.config.SetUpCompleted {
		var missing []string
		if req.LLMAPIKey == "" {
			missing = append(missing, "llm_api_key")
		}
		if req.LLMBaseURL == "" {
			missing = append(missing, "llm_base_url")
		}
		if req.LLMModel == "" {
			missing = append(missing, "llm_model")
		}
		if len(missing) > 0 {
			slog.Warn("wifi-provision: fresh device missing required LLM fields",
				"component", "device", "missing", missing)
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"fresh device requires: "+strings.Join(missing, ", ")))
			return
		}
	}

	// Fresh device with no admin password on file gets the same hardware-suffix
	// default as handler.Setup. Once a hash is on file, the operator's PATCH
	// leaves it alone (empty admin_password = "keep current"). Failing here
	// rather than silently defaulting to a hardcoded value avoids handing every
	// unidentified device the same well-known password.
	if req.AdminPassword == "" && !h.config.SetUpCompleted && h.config.AdminPasswordHash == "" {
		mac := device.GetDeviceMac()
		dash := strings.LastIndex(mac, "-")
		if mac == "" || dash < 0 || dash == len(mac)-1 {
			slog.Warn("wifi-provision: admin_password default failed", "component", "device", "mac", mac)
			c.JSON(http.StatusBadRequest, serializers.ResponseError(
				"device hardware ID unreadable — cannot default admin_password (set it manually)"))
			return
		}
		req.AdminPassword = mac[dash+1:]
	}
	// Set session cookie now so the browser is logged in when it redirects to
	// the new LAN IP post-AP-teardown.
	if req.AdminPassword != "" {
		if err := session.Issue(c, h.config); err != nil {
			slog.Warn("wifi-provision: issue session failed", "component", "device", "error", err)
		}
	}

	// Same 2s pre-delay as Setup so the HTTP response has time to reach the
	// client before the AP tears down mid-request. See handler.Setup.
	go func() {
		time.Sleep(2 * time.Second)
		if err := h.service.ReprovisionWifi(req); err != nil {
			slog.Error("wifi-provision failed", "component", "device", "error", err)
			h.networkService.SwitchToAPMode()
			return
		}
		slog.Info("wifi-provision success", "component", "device")
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
	phase, lanIP, errMsg, run := h.service.SetupStatus()
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
		// Setup runs since boot. The web client compares it against the value it
		// read before submitting to tell its own run's verdict from a leftover —
		// phase alone is not enough when a run resolves inside one poll interval.
		"run": run,
		// Whether this device has ever completed setup. `SetupGate` needs it to
		// choose the initial wizard vs the continue wizard, and it used to infer
		// that from "does the device have internet" — sound only while the
		// provisioning AP was the device's only network, which ethernet is not.
		// A boolean, not a secret: it says nothing beyond what the wizard is
		// about to show, and the endpoint must stay open because a device that
		// hasn't finished setup has no admin password to authenticate against.
		"set_up_completed": h.config.SetUpCompleted,
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

	voices, err := hal.ListVoices(provider, lang)
	if err == nil && len(voices) > 0 {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
		return
	}
	// Piper voices are files under /opt/piper, so HAL is the only thing that
	// can know what is installed — there is no static list to fall back to.
	// Answering an unreachable HAL with an empty success would be a claim this
	// server cannot make ("no voices installed"), and the web takes it as the
	// authoritative list: the picker empties and, since it only refetches on a
	// provider or language change, never fills back in. An error instead leaves
	// the client holding its last known-good list.
	if provider == domain.TTSProviderPiper {
		if err != nil {
			c.JSON(http.StatusServiceUnavailable,
				serializers.ResponseError("hal unreachable: "+err.Error()))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(voices))
		return
	}
	// Fallback to static list (no language filtering — static list is EN-only)
	staticVoices, ok := domain.TTSVoicesByProvider[provider]
	if !ok {
		staticVoices = domain.TTSVoices
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(staticVoices))
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
// switch now BLOCKS until it lands (the reserved switch waits on switch-runtime,
// which may install the backend). HTTP additionally requests an optional runtime
// readiness probe, so a backend such as Hermes is not persisted merely because its
// systemd process is active while its gateway is still booting. We validate the
// runtime synchronously for the 400 and run the switch in the background, returning
// 200 "accepted" right away.
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
	run, err := h.service.ReserveAgentRuntimeSwitchReady(req)
	if err != nil {
		if errors.Is(err, device.ErrAgentRuntimeSwitchInProgress) {
			c.JSON(http.StatusConflict, serializers.ResponseError("agent runtime switch already in progress"))
			return
		}
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	go func() {
		switched, err := run()
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

// GetTimezone returns the device's current IANA timezone plus the selectable
// zone list (from the system tzdata) for the web Settings picker.
//
//	@Router	/device/timezone [get]
func (h *DeviceHandler) GetTimezone(c *gin.Context) {
	current, zones := h.service.GetTimezone()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(domain.TimezoneStatus{
		Current: current,
		Zones:   zones,
	}))
}

// SetTimezone applies an IANA timezone (e.g. "Asia/Ho_Chi_Minh"): writes
// /etc/localtime + /etc/timezone (best-effort timedatectl) and persists it to
// config.json. HAL's clock helpers read /etc/timezone fresh per call, so the
// change takes effect without a HAL restart. An unknown zone returns 400.
//
//	@Router	/device/timezone [post]
func (h *DeviceHandler) SetTimezone(c *gin.Context) {
	var req domain.TimezoneSetData
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.SetTimezone(req.Timezone); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
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

// ListMCPTools returns the configured remote MCP tools.
func (h *DeviceHandler) ListMCPTools(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(h.service.ListMCPTools()))
}

// AddMCPTool adds a remote MCP tool endpoint.
func (h *DeviceHandler) AddMCPTool(c *gin.Context) {
	var req config.MCPTool
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := h.service.AddMCPTool(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// RemoveMCPTool removes a remote MCP tool by name.
func (h *DeviceHandler) RemoveMCPTool(c *gin.Context) {
	name := c.Param("name")
	if err := h.service.RemoveMCPTool(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}
