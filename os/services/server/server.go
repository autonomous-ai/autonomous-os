package server

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/agent"
	"go.autonomous.ai/os/internal/ambient"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/healthwatch"
	"go.autonomous.ai/os/internal/network"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/hal"
	"go.autonomous.ai/os/lib/i18n"
	"go.autonomous.ai/os/lib/logger"
	"go.autonomous.ai/os/lib/mqtt"
	"go.autonomous.ai/os/lib/safego"
	_agentHttpDeliver "go.autonomous.ai/os/server/agent/delivery/http"
	_buddyHttpDeliver "go.autonomous.ai/os/server/buddy/delivery/http"
	"go.autonomous.ai/os/server/config"
	_deviceHttpDeliver "go.autonomous.ai/os/server/device/delivery/http"
	_deviceMQTTDeliver "go.autonomous.ai/os/server/device/delivery/mqtt"
	_healthHttpDeliver "go.autonomous.ai/os/server/health/delivery/http"
	_networkHttpDeliver "go.autonomous.ai/os/server/network/delivery/http"
	_sensingHttpDeliver "go.autonomous.ai/os/server/sensing/delivery/http"
	systemshell "go.autonomous.ai/os/server/system"
)

type Server struct {
	engine *gin.Engine
	config *config.Config

	// handlers
	healthHandler     _healthHttpDeliver.HealthHandler
	networkHandler    _networkHttpDeliver.NetworkHandler
	deviceHandler     _deviceHttpDeliver.DeviceHandler
	deviceMQTTHandler _deviceMQTTDeliver.DeviceMQTTHandler
	agentHandler      _agentHttpDeliver.AgentHandler
	sensingHandler    _sensingHttpDeliver.SensingHandler
	buddyHandler      _buddyHttpDeliver.BuddyHandler

	agentGateway     domain.AgentGateway
	personaMigration *agent.PersonaMigration
	channelReconcile *agent.ChannelReconcile
	mcpReconcile     *agent.MCPReconcile
	networkService   *network.Service
	deviceService    *device.Service
	ambientService   *ambient.Service
	healthWatch      *healthwatch.Service
	statusLED        *statusled.Service

	// mqttFactory is the optional MQTT factory (nil when broker not configured).
	mqttFactory *mqtt.Factory
	// mqttClient is the active MQTT client when setup is complete; guarded by mqttMu.
	mqttClient *mqtt.MQTT
	mqttCancel context.CancelFunc
	mqttMu     sync.Mutex

	// monitorCtx: context for network monitor + status reporter. Created when SetUpCompleted true, cancelled when false or on shutdown.
	monitorCtx context.Context
	// monitorCancel cancels monitorCtx.
	monitorCancel context.CancelFunc
	// monitorMu guards monitorCtx and monitorCancel.
	monitorMu sync.Mutex
	// lastSetupCompleted is the last SetUpCompleted value we acted on. Used to avoid redundant handleSetUpCompleteChanged when config notifies but value unchanged.
	lastSetupCompleted *bool
	// lastDeviceID is the last DeviceID value we acted on. When this changes (typically empty → assigned at first /device/setup), we restart claude-desktop-buddy so its BLE name picks up the new device_id.
	lastDeviceID *string
	// lastMQTTSig is the last MQTT-connection signature we acted on (endpoint +
	// port + username + password + fa_channel). When any of these change — via a
	// status-reporter ping response OR a PUT /api/device/config edit — we restart
	// the MQTT client so it reconnects/resubscribes with the new broker config,
	// without requiring a full device restart.
	lastMQTTSig *string
}

// Engine ...
func (s *Server) Engine() *gin.Engine {
	return s.engine
}

// GetContext ...
func (s *Server) GetContext(c *gin.Context) context.Context {
	ctx := c.Request.Context()
	if ctx == nil {
		ctx = context.Background()
	}

	return ctx
}

func ProvideServer(
	cfg *config.Config,
	hh _healthHttpDeliver.HealthHandler,
	nh _networkHttpDeliver.NetworkHandler,
	dh _deviceHttpDeliver.DeviceHandler,
	dqth _deviceMQTTDeliver.DeviceMQTTHandler,
	agentH _agentHttpDeliver.AgentHandler,
	sensingH _sensingHttpDeliver.SensingHandler,
	buddyH _buddyHttpDeliver.BuddyHandler,
	ds *device.Service,
	agentGW domain.AgentGateway,
	pm *agent.PersonaMigration,
	cr *agent.ChannelReconcile,
	mr *agent.MCPReconcile,
	ns *network.Service,
	mqttFactory *mqtt.Factory,
	ambientSvc *ambient.Service,
	hw *healthwatch.Service,
	sled *statusled.Service,
) *Server {
	return &Server{
		config:            cfg,
		healthHandler:     hh,
		networkHandler:    nh,
		deviceHandler:     dh,
		deviceMQTTHandler: dqth,
		agentHandler:      agentH,
		sensingHandler:    sensingH,
		buddyHandler:      buddyH,
		agentGateway:      agentGW,
		personaMigration:  pm,
		channelReconcile:  cr,
		mcpReconcile:      mr,
		networkService:    ns,
		deviceService:     ds,
		mqttFactory:       mqttFactory,
		ambientService:    ambientSvc,
		healthWatch:       hw,
		statusLED:         sled,
	}
}

func (s *Server) Serve(closeFn func()) error {
	// Device type is mandatory — refuse to boot rather than silently assume a
	// "lamp" (wrong soul/hardware/OTA). Mirrors the fail-loud provisioning layer.
	deviceType := s.config.DeviceTypeOrDefault()
	if deviceType == "" {
		log.Fatal("[config] device_type unresolved — set DEVICE_TYPE env (provisioning) or config.json device_type; refusing to assume 'lamp'")
	}

	// Set GELF host to device_id + stamp device class for centralized logging
	if s.config.DeviceID != "" {
		logger.SetGELFHost(s.config.DeviceID)
	}
	logger.SetGELFDeviceType(deviceType)
	// i18n device name (wake-words + {name}/{Name} in strings) — device_type as the
	// startup fallback; WatchIdentity overrides with the agent name once IDENTITY.md loads.
	i18n.SetDeviceName(deviceType)

	// Register the shared bearer token for outbound HAL HTTP calls.
	// HAL's local_only_middleware accepts Authorization: Bearer <llm_api_key>
	// as one of its allow paths; sending it lets calls succeed even if loopback
	// bypass is tightened later. Empty key drops the header (local LLM mode).
	hal.SetAPIKey(s.config.LLMAPIKey)

	// Signal booting state so the LED shows a slow blue pulse while initializing.
	s.statusLED.Set(statusled.StateBooting)

	// Wire i18n before any TTS-firing goroutine starts. Must precede StartWS
	// below — a WS reconnect that lands before i18n is wired falls back to
	// English even when STTLanguage is "vi"/"zh-*".
	i18n.SetConfig(s.config)

	s.handleSetUpCompleteChange(s.config.SetUpCompleted)
	s.handleDeviceIDChange(s.config.DeviceID)
	s.handleMQTTConfigChange()

	configCtx, cancelConfig := context.WithCancel(context.Background())
	defer cancelConfig()
	go s.runConfigChangeListener(configCtx)

	eventCtx, cancelEvents := context.WithCancel(context.Background())
	defer cancelEvents()
	go s.agentGateway.StartWS(eventCtx, s.agentHandler.HandleEvent)
	go s.agentGateway.WatchIdentity(eventCtx)
	go s.agentGateway.StartSkillWatcher(eventCtx)
	// StartModelSync is launched from the startup-sequence goroutine AFTER
	// EnsureOnboarding completes, so the two writers to openclaw.json don't
	// race on first boot (sync's atomic write vs ensureAgentDefaults' plain
	// os.WriteFile would clobber each other).

	r := gin.Default()
	r.RedirectTrailingSlash = false // avoid 301 redirect loop on /network vs /network/
	r.Use(corsMiddleware())
	r.Use(gin.Recovery())

	api := r.Group("api")

	health := api.Group("health")
	health.GET("/live", s.healthHandler.Live)
	health.GET("/readiness", s.healthHandler.Readiness)

	system := api.Group("system")
	system.GET("info", s.healthHandler.SystemInfo)
	system.GET("network", s.healthHandler.NetworkInfo)
	system.GET("dashboard", s.healthHandler.Dashboard)
	system.POST("software-update/:target", adminAuthMiddleware(s.config), s.softwareUpdate)
	system.POST("factory-reset", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		systemshell.FactoryReset(c, s.agentGateway)
	})
	system.POST("exec", localOnlyMiddleware(), s.execCommand)
	// xterm.js shell: admin-gated. WS upgrade doesn't carry the Bearer header
	// in browsers, so the cookie path inside adminAuthMiddleware is the live
	// auth on this route. Scripts may still ?token=<llm_api_key>=.
	system.GET("shell", adminAuthMiddleware(s.config), systemshell.ShellHandler)

	// Login: POST {password} → bcrypt-verifies admin_password_hash, mints
	// signed session cookie. No auth required (this is how you get auth).
	api.POST("login", s.loginHandler)
	api.POST("logout", s.logoutHandler)
	// Exchange Bearer auth for a session cookie on the current origin.
	// Used by the AP→.local post-setup redirect: os_session is bound to
	// the AP origin and doesn't survive the host switch, so the web carries
	// the Bearer (llm_api_key) across via URL fragment and exchanges it for
	// a cookie here. adminAuthMiddleware already validates the Bearer (or an
	// existing cookie), so the handler just mints a fresh cookie. No new
	// capability vs. Bearer auth — both are root under the shared-secret
	// threat model — purely a UX helper that survives refresh / new tabs.
	api.POST("login/exchange", adminAuthMiddleware(s.config), s.loginExchangeHandler)

	device := api.Group("device")
	device.POST("setup", setupOrAdminMiddleware(s.config), s.deviceHandler.Setup)
	device.GET("setup/status", s.deviceHandler.SetupStatus)
	device.POST("channel", adminAuthMiddleware(s.config), s.deviceHandler.ChangeChannel)
	// GET config is admin-gated now. Pre-login web can no longer bootstrap
	// the bearer from here — browser must POST /api/login first (cookie),
	// scripts/curl must send Authorization: Bearer <llm_api_key>.
	device.GET("config", adminAuthMiddleware(s.config), s.deviceHandler.GetConfig)
	device.PUT("config", adminAuthMiddleware(s.config), s.deviceHandler.UpdateConfig)
	device.GET("voices", s.deviceHandler.GetVoices)
	device.GET("tts-providers", s.deviceHandler.GetTTSProviders)
	device.GET("realtime-options", s.deviceHandler.GetRealtimeOptions)
	device.GET("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.GetAgentRuntime)
	device.POST("agent-runtime", adminAuthMiddleware(s.config), s.deviceHandler.SetAgentRuntime)

	network := api.Group("network")
	network.GET("", s.networkHandler.GetNetworks)
	network.GET("current", s.networkHandler.GetCurrentNetwork)
	network.GET("check-internet", s.networkHandler.CheckInternet)

	sensing := api.Group("sensing")
	sensing.POST("event", sameOriginOrLAN(), s.sensingHandler.PostEvent)
	sensing.GET("snapshot/:category/:name", s.sensingHandler.GetSnapshot)
	sensing.GET("audio/:name", s.sensingHandler.GetAudio)

	// Voice file delete (filesystem orchestration on Pi). Voice enroll
	// itself lives on hal at /hw/speaker/record-enroll because hardware
	// capture is Python's domain.
	voice := api.Group("voice")
	voice.POST("file/remove", s.sensingHandler.RemoveVoiceFile)
	// TTS preview: web ships `{text, voice, provider}` only; server reads
	// the TTS API key + base URL from cfg and forwards to HAL. Replaces
	// the previous web-side `testTTSVoice` that POSTed tts_api_key through
	// the hardware proxy (audit web F13).
	voice.POST("preview", adminAuthMiddleware(s.config), s.voicePreview)

	guard := api.Group("guard")
	guard.POST("enable", s.sensingHandler.EnableGuard)
	guard.POST("disable", s.sensingHandler.DisableGuard)
	guard.GET("", s.sensingHandler.GetGuardStatus)
	guard.POST("alert", sameOriginOrLAN(), s.sensingHandler.PostGuardAlert)

	moodGroup := api.Group("mood")
	moodGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostMoodLog)

	wellbeingGroup := api.Group("wellbeing")
	wellbeingGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostWellbeingLog)

	postureGroup := api.Group("posture")
	postureGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostPostureLog)

	musicSuggGroup := api.Group("music-suggestion")
	musicSuggGroup.POST("log", sameOriginOrLAN(), s.sensingHandler.PostMusicSuggestionLog)
	musicSuggGroup.POST("status", sameOriginOrLAN(), s.sensingHandler.PostMusicSuggestionStatus)

	monitor := api.Group("monitor")
	monitor.POST("event", sameOriginOrLAN(), s.sensingHandler.PostMonitorEvent)

	// Autonomous Buddy (macOS companion app for remote computer use):
	//   - /pair/start, /status, /command, DELETE admin-gated
	//   - /pair/confirm anonymous (code-based)
	//   - /ws bearer-token gated (validated in handler against buddies.json)
	//   - /command localhost-only (OpenClaw skill is the caller)
	buddy := api.Group("buddy")
	buddy.POST("pair/start", adminAuthMiddleware(s.config), s.buddyHandler.PairStart)
	buddy.POST("pair/confirm", s.buddyHandler.PairConfirm)
	buddy.GET("status", adminAuthMiddleware(s.config), s.buddyHandler.Status)
	buddy.DELETE("", adminAuthMiddleware(s.config), s.buddyHandler.Revoke)
	// /self auth via Bearer token (the buddy app's own token), used when the
	// user unpairs from inside the buddy app — symmetric counterpart to the
	// admin DELETE above. Keeps device + buddy state in sync without manual web
	// UI clicks.
	buddy.DELETE("self", s.buddyHandler.RevokeSelf)
	buddy.GET("ws", s.buddyHandler.WS)
	buddy.POST("command", localOnlyMiddleware(), s.buddyHandler.Command)
	// /exec/:action is the marker-friendly variant used by OpenClaw skills via
	// [HW:/buddy/exec/<action>:{...}]. Localhost-only (loopback from agent handler's hwMarker dispatcher).
	buddy.POST("exec/:action", localOnlyMiddleware(), s.buddyHandler.Exec)

	agent := api.Group("agent")
	// Everything under /api/openclaw/ is admin-gated: status carries device
	// state, events / flow-stream / recent / flow-events / flow-logs /
	// analytics / compaction-latest contain conversation history + sensing
	// data, and mood/wellbeing/posture/music-suggestion histories are
	// per-user behavioural records. config-json keeps its stricter
	// `localOnlyMiddleware` (loopback callers only) — admin auth alone is
	// not enough since the raw openclaw.json holds gateway tokens.
	agent.POST("tts/stop", adminAuthMiddleware(s.config), s.agentHandler.StopTTS)
	agent.POST("busy", adminAuthMiddleware(s.config), s.agentHandler.SetBusy)
	agent.GET("status", adminAuthMiddleware(s.config), s.agentHandler.Status)
	agent.GET("events", adminAuthMiddleware(s.config), s.agentHandler.Events)
	agent.GET("recent", adminAuthMiddleware(s.config), s.agentHandler.Recent)
	agent.GET("flow-events", adminAuthMiddleware(s.config), s.agentHandler.FlowEvents)
	agent.GET("mood-history", adminAuthMiddleware(s.config), s.agentHandler.MoodHistory)
	agent.GET("wellbeing-history", adminAuthMiddleware(s.config), s.agentHandler.WellbeingHistory)
	agent.GET("posture-history", adminAuthMiddleware(s.config), s.agentHandler.PostureHistory)
	agent.GET("music-suggestion-history", adminAuthMiddleware(s.config), s.agentHandler.MusicSuggestionHistory)
	agent.GET("flow-stream", adminAuthMiddleware(s.config), s.agentHandler.FlowStream)
	agent.GET("flow-logs", adminAuthMiddleware(s.config), s.agentHandler.FlowLogs)
	agent.DELETE("flow-logs", adminAuthMiddleware(s.config), s.agentHandler.ClearFlowLogs)
	agent.GET("analytics", adminAuthMiddleware(s.config), s.agentHandler.Analytics)
	agent.GET("config-json", localOnlyMiddleware(), s.agentHandler.ConfigJSON)
	// channel-turn: the Hermes gateway observer hook POSTs each turn here so
	// channel (Telegram/Slack/…) turns surface in Flow Monitor. Loopback-only.
	agent.POST("channel-turn", localOnlyMiddleware(), s.agentHandler.ChannelTurn)
	agent.GET("compaction-latest", adminAuthMiddleware(s.config), s.agentHandler.CompactionLatest)

	logs := api.Group("logs")
	logs.GET("tail", adminAuthMiddleware(s.config), s.logTail)
	logs.GET("stream", adminAuthMiddleware(s.config), s.logStream)

	// Wildcard reverse proxy: web UI calls /api/hardware/<anything> with a
	// bearer token; Go gates the request then forwards to HAL on loopback.
	// Replaces direct browser /hw/* access (audit web F5) so nginx /hw/
	// allow 127.0.0.1; deny all; can stay locked down (audit local F2).
	api.Any("/hardware/*path", adminAuthMiddleware(s.config), gin.WrapH(hardwareProxy))

	// Top-level /openapi.json so the in-iframe HAL Swagger UI (loaded at
	// /api/hardware/docs) can fetch its spec — FastAPI hardcodes the spec
	// URL as the absolute path `/openapi.json` in the rendered HTML, so we
	// expose it at the root. Admin-auth gated; cookie auto-attaches in the
	// iframe context. Loopback-only on HAL side already enforced by the
	// proxy's same upstream as `/api/hardware/*`.
	r.GET("/openapi.json", adminAuthMiddleware(s.config), gin.WrapH(openapiProxy))

	slog.Info("server started", "component", "server")

	errChan := make(chan error)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGINT, syscall.SIGTERM)

	srv := &http.Server{
		Addr:    fmt.Sprintf("127.0.0.1:%d", s.config.HttpPort),
		Handler: r,
	}

	// HTTP server is about to listen — booting is done.
	s.statusLED.Clear(statusled.StateBooting)

	// When the device is still in AP/provisioning mode, paint the strip solid
	// white as a visual "ready for WiFi setup" signal. os-server typically reaches
	// this point before HAL's FastAPI is up on :5001 (Python boot is
	// slower — loads rpi_ws281x, SPI, audio, camera), so we poll /health in
	// the background and fire the setup status only once LED hardware reports ready.
	// Skipped post-setup — agent flash + ambient take over from here.
	if !s.config.SetUpCompleted {
		safego.Go("setup-needed-paint", s.waitAndPaintSetupReady)
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil {
			errChan <- err
		}
	}()

	for {
		select {
		case <-stop:
			// The context is used to inform the server it has 5 seconds to finish
			// the request it is currently handling
			cancelConfig()
			s.monitorMu.Lock()
			if s.monitorCancel != nil {
				s.monitorCancel()
			}
			s.monitorMu.Unlock()
			cancelEvents()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := srv.Shutdown(ctx); err != nil {
				log.Fatal("Server forced to shutdown: ", err)
			}
			closeFn()
			return nil
		case err := <-errChan:
			return err
		}
	}
}
