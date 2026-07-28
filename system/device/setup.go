package device

import (
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/statusled"
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

// apSetupIP is wlan0's static address while the device runs the provisioning
// AP (see scripts/provision/setup-ap.sh). The early LAN-IP poll skips it so it
// only ever publishes the STA-side address from the home router's DHCP.
const apSetupIP = "192.168.100.1"

type setupState struct {
	mu    sync.RWMutex
	phase string
	lanIP string
	error string
	// run counts Setup() invocations since boot and is published alongside the
	// phase. The web client can't rely on catching phase="connecting" to know
	// its own run started: on the wired path the network step is a single ping,
	// so "connecting" can come and go between two 600ms polls, and the client
	// would then discard the "connected" verdict as a leftover from an earlier
	// attempt. A counter it can compare against the value it saw before
	// submitting identifies the run regardless of how briefly any phase lasted.
	run int
}

func (st *setupState) snapshot() (phase, ip, errMsg string, run int) {
	st.mu.RLock()
	defer st.mu.RUnlock()
	return st.phase, st.lanIP, st.error, st.run
}

// begin opens a new run: bumps the counter and parks the state at connecting
// with no IP and no error, so nothing from the previous attempt leaks into it.
func (st *setupState) begin() {
	st.mu.Lock()
	st.run++
	st.phase = SetupPhaseConnecting
	st.lanIP = ""
	st.error = ""
	st.mu.Unlock()
}

func (st *setupState) set(phase, ip, errMsg string) {
	st.mu.Lock()
	st.phase = phase
	st.lanIP = ip
	st.error = errMsg
	st.mu.Unlock()
}

// SetupStatus returns the current Setup phase + LAN IP so the web client
// can poll progress through the AP→STA switch. When no Setup run has
// happened (phase=idle) but the device is already on the home network from a
// previous session, fall back to the live address of the default-route
// interface (wlan0 on WiFi, eth0/end0 when wired) so the web
// client can still detect "you're at the AP IP but the device lives at X"
// and redirect.
func (s *Service) SetupStatus() (phase, lanIP, errMsg string, run int) {
	phase, lanIP, errMsg, run = s.setupState.snapshot()
	if lanIP == "" {
		if ip, err := s.networkService.GetCurrentIP(); err == nil {
			lanIP = ip
		}
	}
	return phase, lanIP, errMsg, run
}

// setupWired completes the network phase for a device that already reaches the
// internet without WiFi — in practice an ethernet cable. There are no credentials
// to apply, so the work is (1) proving the uplink is real, and (2) tearing down
// the provisioning AP, which nothing else on this path would do: AP teardown
// lives in device-sta-mode, and the WiFi path only gets there as the last step of
// connect-wifi. Skipping it would leave the device broadcasting its open setup
// hotspot forever.
func (s *Service) setupWired() error {
	if _, err := s.networkService.CheckInternet(); err != nil {
		const msg = "no WiFi credentials given and the device has no working internet connection"
		s.setupState.set(SetupPhaseFailed, "", msg)
		return fmt.Errorf("%s: %w", msg, err)
	}

	// Publish the address BEFORE leaving AP mode. Tearing the AP down restarts
	// dhcpcd, which can briefly interrupt the very connection the client is
	// talking to us over; with lan_ip already in setupState, a client that loses
	// the response can still find the device by polling /api/setup/status.
	ip, ipErr := s.networkService.GetCurrentIP()
	if ipErr != nil || ip == apSetupIP {
		// apSetupIP means the route lookup fell back to wlan0 (the AP's own
		// address) — not a usable LAN address to hand the client.
		slog.Warn("setup: wired path could not resolve a LAN IP", "component", "device", "ip", ip, "error", ipErr)
		ip = ""
	}
	s.setupState.set(SetupPhaseConnected, ip, "")
	slog.Info("setup: existing uplink verified, skipping WiFi join", "component", "device", "lan_ip", ip)

	if err := s.networkService.LeaveAPMode(); err != nil {
		// Non-fatal: the device is on the network and the rest of setup can
		// finish. Logged at error level because a surviving AP is an open
		// hotspot, not a cosmetic leftover — but failing here would abort setup
		// and leave that same AP up anyway, so continuing is strictly better.
		slog.Error("setup: failed to tear down provisioning AP", "component", "device", "error", err)
	}
	return nil
}

// setupWiFi runs the classic provisioning path: push credentials to
// wpa_supplicant, wait for association, and hand the resulting STA address to the
// web client before the AP disappears underneath it.
func (s *Service) setupWiFi(data domain.SetupRequest) error {
	// Blue-blink cue while wlan0 associates with the target Wi-Fi. Mirrors the
	// intern-v1 behavior (openclaw-lobster's led.ConnectionMode on setup entry).
	// Only on this path — a wired setup never associates, so the cue would be a
	// lie that stays lit for the ~2 min the rest of setup takes.
	s.statusLED.Set(statusled.StateWifiConnecting)

	// Early LAN-IP capture: SetupNetwork() blocks up to 60s waiting for
	// internet, but the AP (192.168.100.1) tears down within ~2s of the
	// AP→STA switch — so by the time SetupNetwork returns and we'd normally
	// read the IP, the web client can no longer poll us over the AP. This
	// goroutine polls while SetupNetwork runs and publishes the new STA
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
					if _, prevIP, _, _ := s.setupState.snapshot(); prevIP != ip {
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
		_, prevIP, _, _ := s.setupState.snapshot()
		ip = prevIP
	}
	if ip != "" {
		s.setupState.set(SetupPhaseConnected, ip, "")
		slog.Info("setup: WiFi associated", "component", "device", "lan_ip", ip)
	} else {
		s.setupState.set(SetupPhaseConnected, "", "")
		slog.Warn("setup: WiFi associated but no IP detected", "component", "device", "error", ipErr)
	}
	return nil
}

func (s *Service) Setup(data domain.SetupRequest) error {
	slog.Info("starting setup", "component", "device")
	data.LLMBaseURL = urlnorm.NormalizeBaseURL(data.LLMBaseURL)
	data.STTBaseURL = urlnorm.NormalizeBaseURL(data.STTBaseURL)
	data.TTSBaseURL = urlnorm.NormalizeBaseURL(data.TTSBaseURL)
	s.setupState.begin()

	// Cleared on every return path below so a re-run after a failed setup starts
	// from the neutral status instead of a stuck blinking strip. Safe to call
	// when the cue was never set (the wired path) — Clear on an inactive state
	// is a no-op. No-op too on devices without the `light` capability.
	defer s.statusLED.Clear(statusled.StateWifiConnecting)

	// Network phase, one of two shapes. An empty SSID means the operator is
	// telling us the device already has a working uplink and needs no WiFi —
	// the ethernet case — so we verify that claim and leave the AP instead of
	// running a join. Everything after this block is identical either way.
	if strings.TrimSpace(data.SSID) == "" {
		if err := s.setupWired(); err != nil {
			return err
		}
	} else if err := s.setupWiFi(data); err != nil {
		return err
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

	// Early presence ping — fire-and-forget: publish the freshly-acquired STA
	// IP to the backend the moment WiFi + config are ready, WITHOUT waiting for
	// the agent setup below (up to ~2 min). The page that opened the Setup
	// popup polls the backend for this IP and redirects the popup when neither
	// the AP-alive window nor mDNS could deliver it (see docs/setup-flow.md).
	// Must run after config.Save above: beclient derives the ping URL from the
	// just-assigned LLMBaseURL.
	if s.beClient != nil && llmAPIKey != "" {
		go func() { s.beClient.PingSafe(llmAPIKey, s.buildPingPayload("setting_up")) }()
	}

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
		s.beClient.PingSafe(llmAPIKey, s.buildPingPayload("working"))
	}
	return nil
}
