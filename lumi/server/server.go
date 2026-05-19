package server

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/ambient"
	"go-lamp.autonomous.ai/internal/device"
	"go-lamp.autonomous.ai/internal/healthwatch"
	"go-lamp.autonomous.ai/internal/network"
	"go-lamp.autonomous.ai/internal/statusled"
	devicebutton "go-lamp.autonomous.ai/lib/devicebutton"
	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/lib/lelamp"
	"go-lamp.autonomous.ai/lib/logger"
	"go-lamp.autonomous.ai/lib/mqtt"
	"go-lamp.autonomous.ai/lib/safego"
	"go-lamp.autonomous.ai/server/config"
	_deviceGPIODeliver "go-lamp.autonomous.ai/server/device/delivery/gpio"
	_deviceHttpDeliver "go-lamp.autonomous.ai/server/device/delivery/http"
	_deviceMQTTDeliver "go-lamp.autonomous.ai/server/device/delivery/mqtt"
	_healthHttpDeliver "go-lamp.autonomous.ai/server/health/delivery/http"
	_networkHttpDeliver "go-lamp.autonomous.ai/server/network/delivery/http"
	_openclawSseDeliver "go-lamp.autonomous.ai/server/openclaw/delivery/sse"
	_sensingHttpDeliver "go-lamp.autonomous.ai/server/sensing/delivery/http"
	"go-lamp.autonomous.ai/server/serializers"
	systemshell "go-lamp.autonomous.ai/server/system"
)

type Server struct {
	engine *gin.Engine
	config *config.Config

	// handlers
	healthHandler     _healthHttpDeliver.HealthHandler
	networkHandler    _networkHttpDeliver.NetworkHandler
	deviceHandler     _deviceHttpDeliver.DeviceHandler
	deviceMQTTHandler _deviceMQTTDeliver.DeviceMQTTHandler
	deviceGPIOHandler _deviceGPIODeliver.DeviceGPIOHandler
	openclawHandler   _openclawSseDeliver.OpenClawHandler
	sensingHandler    _sensingHttpDeliver.SensingHandler

	agentGateway   domain.AgentGateway
	networkService *network.Service
	deviceService  *device.Service
	ambientService *ambient.Service
	healthWatch    *healthwatch.Service
	statusLED      *statusled.Service

	// resetButton watches GPIO 23 for press-and-hold >= 10s to trigger factory reset. Nil when GPIO unavailable.
	deviceButton *devicebutton.DeviceButton
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
	// lastDeviceID is the last DeviceID value we acted on. When this changes (typically empty → assigned at first /device/setup), we restart lumi-buddy so its BLE name picks up the new device_id.
	lastDeviceID *string
	// lastMQTTEndpoint is the last MQTTEndpoint value we acted on. When this changes (typically empty → assigned via status-reporter ping response), we restart the MQTT client so it picks up the new broker config without requiring a full lumi restart.
	lastMQTTEndpoint *string
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
	dgph _deviceGPIODeliver.DeviceGPIOHandler,
	openclawH _openclawSseDeliver.OpenClawHandler,
	sensingH _sensingHttpDeliver.SensingHandler,
	ds *device.Service,
	agentGW domain.AgentGateway,
	ns *network.Service,
	deviceBtn *devicebutton.DeviceButton,
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
		deviceGPIOHandler: dgph,
		openclawHandler:   openclawH,
		sensingHandler:    sensingH,
		agentGateway:      agentGW,
		networkService:    ns,
		deviceService:     ds,
		deviceButton:      deviceBtn,
		mqttFactory:       mqttFactory,
		ambientService:    ambientSvc,
		healthWatch:       hw,
		statusLED:         sled,
	}
}

// restartMQTT stops the current MQTT client and starts a new one (e.g. when backend pushes new MQTT config).
func (s *Server) restartMQTT() {
	s.stopMQTT()
	s.startMQTT()
}

// startMQTT creates a client from the factory, subscribes to the topic, and connects. Idempotent if already running.
func (s *Server) startMQTT() {
	s.mqttMu.Lock()
	if s.mqttClient != nil {
		s.mqttMu.Unlock()
		return
	}
	if s.mqttFactory == nil {
		s.mqttMu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	client := s.mqttFactory.GetClient("lumi-server-" + s.config.DeviceID)
	client.Subscribe(s.config.FAChannel, 1, func(topic string, payload []byte) {
		slog.Debug("message received", "component", "mqtt", "topic", topic, "payload", string(payload))
		s.deviceMQTTHandler.HandleMessage(topic, payload)
	})
	s.mqttClient = client
	s.mqttCancel = cancel
	s.mqttMu.Unlock()

	safego.Go("mqtt", func() {
		if err := client.Connect(ctx); err != nil && ctx.Err() == nil {
			slog.Error("connect failed", "component", "mqtt", "error", err)
		}
	})
}

// stopMQTT disconnects and clears the MQTT client. Safe to call when not connected.
func (s *Server) stopMQTT() {
	s.mqttMu.Lock()
	client := s.mqttClient
	cancel := s.mqttCancel
	s.mqttClient = nil
	s.mqttCancel = nil
	s.mqttMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if client != nil {
		_ = client.Close()
	}
}

// sameOriginOrLAN blocks the route for callers that are neither on the local
// network nor sending a same-origin browser header (Origin/Referer). This lets
// the web UI and Swagger call the endpoint from any IP, while raw curl/Postman
// requests without an Origin header are rejected when coming from outside LAN.
func sameOriginOrLAN() gin.HandlerFunc {
	return func(c *gin.Context) {
		if strings.ToLower(strings.TrimSpace(os.Getenv("LELAMP_MODE"))) == "developer" {
			c.Next()
			return
		}
		// nginx proxies to Go on localhost, so RemoteAddr is always 127.0.0.1.
		// Use X-Real-IP (set by nginx) to get the real client IP.
		clientIP := strings.TrimSpace(c.GetHeader("X-Real-IP"))
		if clientIP == "" {
			// Fallback: first entry of X-Forwarded-For
			clientIP = strings.TrimSpace(strings.SplitN(c.GetHeader("X-Forwarded-For"), ",", 2)[0])
		}
		if clientIP == "" {
			remoteHost, _, _ := net.SplitHostPort(c.Request.RemoteAddr)
			clientIP = remoteHost
		}
		if ip := net.ParseIP(clientIP); ip != nil && (ip.IsLoopback() || ip.IsPrivate()) {
			c.Next()
			return
		}
		deviceHost := c.Request.Host
		origin := strings.SplitN(c.GetHeader("Origin"), ",", 2)[0]
		referer := strings.SplitN(c.GetHeader("Referer"), ",", 2)[0]
		if isAllowedOrigin(origin, deviceHost) || isAllowedOrigin(referer, deviceHost) {
			c.Next()
			return
		}
		c.JSON(http.StatusForbidden, gin.H{"status": 0, "message": "same-origin or LAN only"})
		c.Abort()
	}
}

func goSameOrigin(header, host string) bool {
	if header == "" || host == "" {
		return false
	}
	h := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(header), "https://"), "http://")
	h = strings.SplitN(h, "/", 2)[0]
	return h == host
}

// isAllowedOrigin returns true for same-host origins and approved external
// domains (autonomous.ai subdomains for parent-app iframe embedding).
func isAllowedOrigin(origin, requestHost string) bool {
	if origin == "" {
		return false
	}
	h := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(origin), "https://"), "http://")
	h = strings.SplitN(h, "/", 2)[0]
	// Same host (any IP or .local name the device is reached on).
	return h == requestHost
}

func corsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		if isAllowedOrigin(origin, c.Request.Host) {
			c.Header("Access-Control-Allow-Origin", origin)
			c.Header("Vary", "Origin")
			c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
			c.Header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept, Authorization")
		}
		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}

func (s *Server) Serve(closeFn func()) error {
	// Set GELF host to device_id for centralized logging
	if s.config.DeviceID != "" {
		logger.SetGELFHost(s.config.DeviceID)
	}

	// Signal booting state so the LED shows a slow blue pulse while initializing.
	s.statusLED.Set(statusled.StateBooting)

	// Wire i18n before any TTS-firing goroutine starts. Must precede StartWS
	// below — a WS reconnect that lands before i18n is wired falls back to
	// English even when STTLanguage is "vi"/"zh-*".
	i18n.SetConfig(s.config)

	// device button — disabled here so lelamp (Python) gpio_button can grab
	// GPIO17. Long-press shutdown w/ servo release lives in lelamp.
	// if err := s.deviceButton.Init(); err == nil {
	// 	s.deviceButton.Start(context.Background(), s.deviceGPIOHandler.HandlePress, s.deviceGPIOHandler.HandlePressAndHold)
	// 	defer s.deviceButton.Close()
	// } else {
	// 	slog.Info("[device button] can not init")
	// }

	s.handleSetUpCompleteChange(s.config.SetUpCompleted)
	s.handleDeviceIDChange(s.config.DeviceID)
	s.handleMQTTEndpointChange(s.config.MQTTEndpoint)

	configCtx, cancelConfig := context.WithCancel(context.Background())
	defer cancelConfig()
	go s.runConfigChangeListener(configCtx)

	eventCtx, cancelEvents := context.WithCancel(context.Background())
	defer cancelEvents()
	go s.agentGateway.StartWS(eventCtx, s.openclawHandler.HandleEvent)
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
	system.POST("software-update/:target", s.softwareUpdate)
	system.POST("exec", s.execCommand)
	system.GET("shell", systemshell.ShellHandler)

	device := api.Group("device")
	device.POST("setup", s.deviceHandler.Setup)
	device.GET("setup/status", s.deviceHandler.SetupStatus)
	device.POST("channel", s.deviceHandler.ChangeChannel)
	device.GET("config", s.deviceHandler.GetConfig)
	device.PUT("config", s.deviceHandler.UpdateConfig)
	device.GET("voices", s.deviceHandler.GetVoices)
	device.GET("tts-providers", s.deviceHandler.GetTTSProviders)

	network := api.Group("network")
	network.GET("", s.networkHandler.GetNetworks)
	network.GET("current", s.networkHandler.GetCurrentNetwork)
	network.GET("check-internet", s.networkHandler.CheckInternet)

	sensing := api.Group("sensing")
	sensing.POST("event", sameOriginOrLAN(), s.sensingHandler.PostEvent)
	sensing.GET("snapshot/:category/:name", s.sensingHandler.GetSnapshot)

	// Voice file delete (filesystem orchestration on Pi). Voice enroll
	// itself lives on lelamp at /hw/speaker/record-enroll because hardware
	// capture is Python's domain.
	voice := api.Group("voice")
	voice.POST("file/remove", s.sensingHandler.RemoveVoiceFile)

	guard := api.Group("guard")
	guard.POST("enable", s.sensingHandler.EnableGuard)
	guard.POST("disable", s.sensingHandler.DisableGuard)
	guard.GET("", s.sensingHandler.GetGuardStatus)
	guard.POST("alert", s.sensingHandler.PostGuardAlert)

	moodGroup := api.Group("mood")
	moodGroup.POST("log", s.sensingHandler.PostMoodLog)

	wellbeingGroup := api.Group("wellbeing")
	wellbeingGroup.POST("log", s.sensingHandler.PostWellbeingLog)

	postureGroup := api.Group("posture")
	postureGroup.POST("log", s.sensingHandler.PostPostureLog)

	musicSuggGroup := api.Group("music-suggestion")
	musicSuggGroup.POST("log", s.sensingHandler.PostMusicSuggestionLog)
	musicSuggGroup.POST("status", s.sensingHandler.PostMusicSuggestionStatus)

	monitor := api.Group("monitor")
	monitor.POST("event", s.sensingHandler.PostMonitorEvent)

	oc := api.Group("openclaw")
	oc.POST("tts/stop", s.openclawHandler.StopTTS)
	oc.POST("busy", s.openclawHandler.SetBusy)
	oc.GET("status", s.openclawHandler.Status)
	oc.GET("events", s.openclawHandler.Events)
	oc.GET("recent", s.openclawHandler.Recent)
	oc.GET("flow-events", s.openclawHandler.FlowEvents)
	oc.GET("mood-history", s.openclawHandler.MoodHistory)
	oc.GET("wellbeing-history", s.openclawHandler.WellbeingHistory)
	oc.GET("posture-history", s.openclawHandler.PostureHistory)
	oc.GET("music-suggestion-history", s.openclawHandler.MusicSuggestionHistory)
	oc.GET("flow-stream", s.openclawHandler.FlowStream)
	oc.GET("flow-logs", s.openclawHandler.FlowLogs)
	oc.DELETE("flow-logs", s.openclawHandler.ClearFlowLogs)
	oc.GET("analytics", s.openclawHandler.Analytics)
	oc.GET("config-json", sameOriginOrLAN(), s.openclawHandler.ConfigJSON)
	oc.GET("compaction-latest", s.openclawHandler.CompactionLatest)

	logs := api.Group("logs")
	logs.GET("tail", s.logTail)
	logs.GET("stream", s.logStream)

	slog.Info("server started", "component", "server")

	errChan := make(chan error)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGINT, syscall.SIGTERM)

	srv := &http.Server{
		Addr:    fmt.Sprintf(":%d", s.config.HttpPort),
		Handler: r,
	}

	// HTTP server is about to listen — booting is done.
	s.statusLED.Clear(statusled.StateBooting)

	// When the device is still in AP/provisioning mode, paint the strip solid
	// white as a visual "ready for WiFi setup" signal. lumi typically reaches
	// this point before LeLamp's FastAPI is up on :5001 (Python boot is
	// slower — loads rpi_ws281x, SPI, audio, camera), so we poll /health in
	// the background and fire SetSolid only once LED hardware reports ready.
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

// runConfigChangeListener listens for config changes and calls handleSetUpCompleteChange only when SetUpCompleted changed.
func (s *Server) runConfigChangeListener(ctx context.Context) {
	ch := s.config.GetNotifyChannel()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ch:
			s.handleSetUpCompleteChange(s.config.SetUpCompleted)
			s.handleDeviceIDChange(s.config.DeviceID)
			s.handleMQTTEndpointChange(s.config.MQTTEndpoint)
		}
	}
}

// handleDeviceIDChange restarts lumi-buddy when device_id changes. Buddy's
// BLE name is now derived from the hardware MAC suffix (Claude-lumi-{MAC}) so the
// restart isn't needed for name resolution, but a device_id transition is
// still a useful signal that the device has been re-provisioned — restarting
// buddy clears any stale BLE pairing state from the previous identity.
//
// On the first call (startup bootstrap) we just record the current value
// without restarting — only later transitions trigger a restart.
//
// Best-effort: if lumi-buddy isn't installed (systemctl returns non-zero) we
// log and move on.
func (s *Server) handleDeviceIDChange(deviceID string) {
	if s.lastDeviceID == nil {
		s.lastDeviceID = &deviceID
		return
	}
	if *s.lastDeviceID == deviceID {
		return
	}
	prev := *s.lastDeviceID
	s.lastDeviceID = &deviceID

	slog.Info("device_id changed, restarting lumi-buddy", "component", "config", "old", prev, "new", deviceID)
	safego.Go("lumi-buddy-restart", func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()

		// Skip silently if lumi-buddy isn't installed on this Pi. `systemctl cat`
		// exits non-zero when the unit doesn't exist; that's expected on lamps
		// without the buddy plugin and we don't want to spam logs there.
		if err := exec.CommandContext(ctx, "systemctl", "cat", "lumi-buddy.service").Run(); err != nil {
			return
		}

		out, err := exec.CommandContext(ctx, "systemctl", "restart", "lumi-buddy").CombinedOutput()
		if err != nil {
			slog.Warn("lumi-buddy restart failed", "component", "config", "error", err, "output", strings.TrimSpace(string(out)))
			return
		}
		slog.Info("lumi-buddy restarted", "component", "config")
	})
}

// handleMQTTEndpointChange restarts the MQTT client when MQTTEndpoint changes,
// so a backend-pushed broker config (delivered via status-reporter ping response)
// is picked up without requiring a full lumi restart.
//
// On the first call (startup bootstrap) we just record the current value
// without restarting — handleSetUpCompleteChange already brings MQTT up on
// the initial setup-completed flip, so we only need to act on later changes.
func (s *Server) handleMQTTEndpointChange(endpoint string) {
	if s.lastMQTTEndpoint == nil {
		s.lastMQTTEndpoint = &endpoint
		return
	}
	if *s.lastMQTTEndpoint == endpoint {
		return
	}
	prev := *s.lastMQTTEndpoint
	s.lastMQTTEndpoint = &endpoint

	slog.Info("mqtt endpoint changed, restarting mqtt client", "component", "config", "old", prev, "new", endpoint)
	s.restartMQTT()
}

// waitAndPaintSetupReady polls LeLamp /health up to 30s; when LED hardware
// reports ready it paints the strip solid white as the "device awaiting WiFi
// setup" cue. Exits early if setup completes mid-wait so we don't repaint
// over the post-setup user/agent LED state. Best-effort — silent when LeLamp
// never reports LED ready within budget (logs a warning).
//
// Why this is a poll loop and not a single SetSolid call: lumi-server binds
// :5000 faster than LeLamp's FastAPI binds :5001 on cold boot, so a fire-
// and-forget paint at L<see Serve> would silently drop on connection refused
// and leave the strip dark — exactly when the user needs the "ready for AP"
// signal most.
func (s *Server) waitAndPaintSetupReady() {
	deadline := time.Now().Add(30 * time.Second)
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for time.Now().Before(deadline) {
		if s.config.SetUpCompleted {
			return
		}
		if h, err := lelamp.GetHealth(); err == nil && h.LED {
			lelamp.SetSolid(255, 255, 255)
			slog.Info("setup-needed white painted", "component", "server")
			return
		}
		<-ticker.C
	}
	slog.Warn("setup-needed paint skipped: lelamp LED not ready within 30s", "component", "server")
}

// handleSetUpCompleteChange starts or stops the network monitor and status reporter based on SetUpCompleted.
// When true: cancels any previous monitor context, creates a new one, starts monitor and reporter, and runs OpenClaw ready check.
// When false: cancels monitor/reporter (they exit on ctx.Done()) and switches to AP mode.
func (s *Server) handleSetUpCompleteChange(setupCompleted bool) {
	if s.lastSetupCompleted != nil && *s.lastSetupCompleted == setupCompleted {
		return
	}
	if setupCompleted {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
		}
		s.monitorCtx, s.monitorCancel = context.WithCancel(context.Background())
		s.monitorMu.Unlock()

		slog.Info("setup completed, starting internet monitor", "component", "config")
		s.networkService.StartNetworkMonitor(s.monitorCtx,
			func() { s.statusLED.Set(statusled.StateConnectivity) },
			func() { s.statusLED.Clear(statusled.StateConnectivity) },
		)
		slog.Info("setup completed, starting status reporter", "component", "config")
		safego.Go("status-reporter", func() { s.deviceService.StartStatusReporter(s.monitorCtx) })

		s.restartMQTT()

		safego.Go("startup-sequence", func() {
			// Seed SOUL.md + IDENTITY.md into workspace (factory defaults, once only)
			if err := s.agentGateway.EnsureOnboarding(); err != nil {
				slog.Error("onboarding seed failed", "component", "server", "error", err)
			}

			// Start the periodic model sync only AFTER onboarding finishes —
			// both touch openclaw.json (ensureAgentDefaults via os.WriteFile,
			// sync via atomic tmp+rename); running them concurrently would
			// race and could clobber sync's writes.
			safego.Go("model-sync", func() { s.agentGateway.StartModelSync(s.monitorCtx) })

			if ok := s.deviceService.WaitForAgentReady(120 * time.Second); ok {
				slog.Info("agent gateway ready", "component", "server")
				s.statusLED.FlashReady()
			} else {
				slog.Warn("agent gateway ready timeout", "component", "server")
			}
			// Start voice pipeline on LeLamp (if Deepgram key configured)
			// Retry because lumi-lelamp may not be running yet at setup time.
			if s.config.DeepgramAPIKey != "" {
				for attempt := 1; attempt <= 10; attempt++ {
					err := s.agentGateway.StartLeLampVoice(s.config.DeepgramAPIKey, s.config.LLMAPIKey, s.config.GetSTTAPIKey(), s.config.GetTTSAPIKey(), s.config.LLMBaseURL, s.config.GetSTTBaseURL(), s.config.GetTTSBaseURL(), s.config.TTSVoice, s.config.TTSInstructions, s.config.TTSProvider)
					if err == nil {
						break
					}
					slog.Warn("start LeLamp voice failed", "component", "server", "attempt", attempt, "maxAttempts", 10, "error", err)
					time.Sleep(5 * time.Second)
				}
			}

			// Init speaker volume — set to max so hardware/alsactl level is the effective control.
			if err := s.agentGateway.SetVolume(100); err != nil {
				slog.Warn("init volume failed", "component", "server", "error", err)
			}

			// Greet user now that agent + voice pipeline are ready.
			// Prompt is localized by STTLanguage so the very first turn
			// lands in the owner's language without relying on the agent
			// to translate the priming message.
			if _, err := s.agentGateway.SendSystemChatMessage(wakeGreetingPrompt()); err != nil {
				slog.Warn("startup greeting failed", "component", "server", "error", err)
			}

			// Prewarm dead-air filler WAV cache so the first filler fire is
			// a cache hit (~50ms) instead of a 1.5s ElevenLabs roundtrip.
			// Runs in a goroutine because rendering ~17 phrases serially can
			// take 30-60s and must not block the boot greeting.
			safego.Go("prewarm-fillers", func() { _sensingHttpDeliver.PrewarmFillers() })
			// Start ambient life behaviors (breathing LED, micro-movements, mumbles)
			safego.Go("ambient", func() { s.ambientService.Start(s.monitorCtx) })
			// Watch LeLamp component health; auto-restart voice on ALSA failure
			safego.Go("healthwatch", func() { s.healthWatch.Start(s.monitorCtx) })
		})
	} else {
		s.monitorMu.Lock()
		if s.monitorCancel != nil {
			s.monitorCancel()
			s.monitorCancel = nil
		}
		s.monitorMu.Unlock()
		s.stopMQTT()
		s.networkService.SwitchToAPMode()
	}
	s.lastSetupCompleted = &setupCompleted
}

// allowedLogs maps source names to their log file paths (supports glob patterns).
// Entries prefixed with "journal:" use journalctl instead of file reading.
var allowedLogs = map[string]string{
	"lelamp":           "/var/log/lelamp/server.log",
	"lumi":             "/var/log/lumi.log",
	"openclaw":         "/var/log/openclaw/lumi.log",
	"openclaw-service": "journal:openclaw.service",
	"buddy":            "/var/log/lumi-buddy.log",
}

// resolveOpenclawLog returns the openclaw log path, falling back to the newest
// file in /tmp/openclaw/ when the configured path does not exist.
func resolveOpenclawLog() string {
	primary := allowedLogs["openclaw"]
	if info, err := os.Stat(primary); err == nil && info.Size() > 0 {
		return primary
	}
	matches, _ := filepath.Glob("/tmp/openclaw/openclaw-*.log")
	if len(matches) == 0 {
		return primary
	}
	sort.Strings(matches)
	return matches[len(matches)-1] // newest date-stamped file
}

// resolveLogPaths expands a pattern (plain path or glob) to matching files.
func resolveLogPaths(pattern string) ([]string, error) {
	if !strings.ContainsAny(pattern, "*?[") {
		return []string{pattern}, nil
	}
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return nil, fmt.Errorf("glob: %w", err)
	}
	sort.Strings(matches)
	return matches, nil
}

// logTail returns the last N lines of a whitelisted log file (or merged glob).
// GET /api/logs/tail?source=lelamp|lumi|openclaw|openclaw-service&lines=200
func (s *Server) logTail(c *gin.Context) {
	source := c.DefaultQuery("source", "lumi")
	pattern, ok := allowedLogs[source]
	if !ok {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown log source"))
		return
	}

	n, _ := strconv.Atoi(c.DefaultQuery("lines", "200"))
	if n <= 0 || n > 5000 {
		n = 200
	}

	// Journal-based source: use journalctl instead of file reading.
	if strings.HasPrefix(pattern, "journal:") {
		unit := strings.TrimPrefix(pattern, "journal:")
		lines, err := journalTail(unit, n)
		errMsg := ""
		if err != nil {
			errMsg = err.Error()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"source": source,
			"path":   "journalctl -u " + unit,
			"lines":  lines,
			"error":  errMsg,
		}))
		return
	}

	if source == "openclaw" {
		pattern = resolveOpenclawLog()
	}

	paths, err := resolveLogPaths(pattern)
	if err != nil || len(paths) == 0 {
		errMsg := "no log files found"
		if err != nil {
			errMsg = err.Error()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"source": source,
			"path":   pattern,
			"lines":  []string{},
			"error":  errMsg,
		}))
		return
	}

	var allLines []string
	for _, p := range paths {
		lines, _ := tailFile(p, n)
		allLines = append(allLines, lines...)
	}
	// Keep only last n lines across all files
	if len(allLines) > n {
		allLines = allLines[len(allLines)-n:]
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"source": source,
		"path":   pattern,
		"lines":  allLines,
	}))
}

// logStream streams new log lines via SSE from one or more log files.
// GET /api/logs/stream?source=lelamp|lumi|openclaw|openclaw-service
func (s *Server) logStream(c *gin.Context) {
	source := c.DefaultQuery("source", "lumi")
	pattern, ok := allowedLogs[source]
	if !ok {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown log source"))
		return
	}

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	// Journal-based source: stream via journalctl -f.
	if strings.HasPrefix(pattern, "journal:") {
		unit := strings.TrimPrefix(pattern, "journal:")
		s.streamJournal(c, unit)
		return
	}

	if source == "openclaw" {
		pattern = resolveOpenclawLog()
	}

	paths, err := resolveLogPaths(pattern)
	if err != nil || len(paths) == 0 {
		errMsg := "no log files found"
		if err != nil {
			errMsg = err.Error()
		}
		c.SSEvent("error", errMsg)
		return
	}

	type fileTail struct {
		f      *os.File
		reader *bufio.Reader
	}
	var tails []fileTail
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil {
			continue
		}
		// Seek to end
		_, _ = f.Seek(0, 2)
		tails = append(tails, fileTail{f: f, reader: bufio.NewReader(f)})
	}
	if len(tails) == 0 {
		c.SSEvent("error", "cannot open any log files")
		return
	}
	defer func() {
		for _, t := range tails {
			t.f.Close()
		}
	}()

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	c.Stream(func(w io.Writer) bool {
		select {
		case <-c.Request.Context().Done():
			return false
		case <-ticker.C:
			for i := range tails {
				for {
					line, err := tails[i].reader.ReadString('\n')
					if len(line) > 0 {
						c.SSEvent("log", strings.TrimRight(line, "\n"))
					}
					if err != nil {
						break
					}
				}
			}
			return true
		}
	})
}

// journalTail returns the last n lines from a systemd journal unit.
func journalTail(unit string, n int) ([]string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "journalctl", "-u", unit, "--no-pager", "-n", strconv.Itoa(n))
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("journalctl: %w", err)
	}
	var lines []string
	for _, line := range strings.Split(string(out), "\n") {
		if line != "" {
			lines = append(lines, line)
		}
	}
	return lines, nil
}

// streamJournal streams journal lines via SSE using journalctl -f.
func (s *Server) streamJournal(c *gin.Context, unit string) {
	ctx := c.Request.Context()
	cmd := exec.CommandContext(ctx, "journalctl", "-u", unit, "--no-pager", "-f", "-n", "0")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		c.SSEvent("error", "journalctl pipe: "+err.Error())
		return
	}
	if err := cmd.Start(); err != nil {
		c.SSEvent("error", "journalctl start: "+err.Error())
		return
	}
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()

	scanner := bufio.NewScanner(stdout)
	lineCh := make(chan string, 64)
	go func() {
		defer close(lineCh)
		for scanner.Scan() {
			lineCh <- scanner.Text()
		}
	}()

	c.Stream(func(w io.Writer) bool {
		select {
		case <-ctx.Done():
			return false
		case line, ok := <-lineCh:
			if !ok {
				return false
			}
			c.SSEvent("log", line)
			// Drain any buffered lines to batch SSE writes.
			for {
				select {
				case l, ok := <-lineCh:
					if !ok {
						return false
					}
					c.SSEvent("log", l)
				default:
					return true
				}
			}
		}
	})
}

// softwareUpdate triggers an OTA update for a single named component via the bootstrap worker.
// POST /api/system/software-update/:target  (target: lumi | web | lelamp)
func (s *Server) softwareUpdate(c *gin.Context) {
	target := c.Param("target")
	allowed := map[string]bool{"lumi": true, "web": true, "lelamp": true}
	if !allowed[target] {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown target: "+target))
		return
	}
	url := "http://127.0.0.1:8080/force-check/" + target
	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodPost, url, nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("build request: "+err.Error()))
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	c.JSON(http.StatusOK, serializers.ResponseSuccess("software update triggered: "+target))
}

// execCommand runs a shell command (sh -c) and returns stdout, stderr, and exit code.
// POST /api/system/exec  body: {"cmd": "..."}
func (s *Server) execCommand(c *gin.Context) {
	var body struct {
		Cmd string `json:"cmd"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.Cmd) == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("cmd required"))
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", body.Cmd)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	exitCode := 0
	if err := cmd.Run(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = -1
			if stderr.Len() == 0 {
				stderr.WriteString(err.Error())
			}
		}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"stdout":    stdout.String(),
		"stderr":    stderr.String(),
		"exit_code": exitCode,
	}))
}

// tailFile reads the last n lines from a single file.
func tailFile(path string, n int) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 256*1024), 256*1024)

	var ring []string
	for scanner.Scan() {
		ring = append(ring, scanner.Text())
		if len(ring) > n {
			ring = ring[1:]
		}
	}
	if err := scanner.Err(); err != nil {
		return ring, fmt.Errorf("scan: %w", err)
	}
	return ring, nil
}
