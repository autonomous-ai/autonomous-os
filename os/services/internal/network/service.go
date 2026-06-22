package network

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

const (
	defaultInterface = "wlan0"

	// Network monitor: after N consecutive ping failures, set LED to WorkingNoInternet.
	// Use forgiving timeouts/counts so brief WiFi hiccups don't flip to no-internet.
	networkMonitorPingTarget    = "8.8.8.8"
	networkMonitorFailsRequired = 5
	networkMonitorInterval      = 5 * time.Second
	networkMonitorPingTimeout   = 3 * time.Second
	// After this many consecutive failures, attempt WiFi reconnect.
	networkMonitorReconnectAt       = 10 // ~50s of downtime
	networkMonitorReconnectCooldown = 2 * time.Minute
	// After this many reconnect failures, reboot the device.
	networkMonitorMaxReconnects = 5 // 5 attempts × 2min cooldown = ~10min before reboot
)

// Service provides network scan, current network, and setup. When wifiManager is non-nil (production Pi),
// it uses iw for scan and delegates current/setup to the wifi manager (no NetworkManager).
type Service struct {
	config   *config.Config
	networks []domain.Network

	// network monitor state (guarded by networkMonitorMu)
	networkMonitorMu          sync.Mutex
	networkMonitorConsecutive int

	// connectivity callbacks; set once by StartNetworkMonitor before the goroutine starts.
	onConnectivityLost     func()
	onConnectivityRestored func()

	lastReconnectAttempt time.Time
	reconnectAttempts    int
}

// ProvideService returns a network service. Pass nil for wifiManager when not using WiFi manager (e.g. dev with NM).
func ProvideService(config *config.Config) *Service {
	return &Service{
		config:   config,
		networks: []domain.Network{},
	}
}

// ListNetworks returns visible WiFi networks. When using wifi manager, runs iw dev wlan0 scan (STA mode only).
func (s *Service) ListNetworks() ([]domain.Network, error) {
	return s.listNetworksIW()
}

// listNetworksIW runs `iw dev wlan0 scan` and parses BSS/SSID/signal etc.
func (s *Service) listNetworksIW() ([]domain.Network, error) {
	slog.Debug("wifi scan started", "component", "network")
	cmd := exec.Command("iw", "dev", defaultInterface, "scan")
	var outBuf, errBuf bytes.Buffer
	cmd.Stdout = &outBuf
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("iw scan: %w", err)
	}
	networks := parseIWScan(outBuf.String())
	s.networks = networks
	slog.Debug("wifi scan done", "component", "network")
	return networks, nil
}

var (
	reBSS    = regexp.MustCompile(`BSS ([0-9a-f:]+)`)
	reSSID   = regexp.MustCompile(`SSID: (.+)`)
	reSignal = regexp.MustCompile(`signal: ([\d.-]+)`)
	reTxRate = regexp.MustCompile(`tx bitrate:\s*([\d.]+)\s*MBit/s`)
	reDS     = regexp.MustCompile(`DS Parameter set: channel (\d+)`)
	reInet   = regexp.MustCompile(`inet (\d+\.\d+\.\d+\.\d+)`)
)

// decodeIWSSIDEscape reverses the byte-escape format that `iw` (and wpa_cli)
// emit for SSIDs containing non-printable / non-ASCII bytes — required for
// Chinese (UTF-8 3-byte chars) and other non-Latin SSIDs. iw prints those
// bytes as `\xNN` and leading/trailing space as `\ `; printable ASCII passes
// through unchanged. Without decoding, the scan list shows literal `\xE4...`
// and the post-connect SSID comparison in SetupNetwork fails byte-equality
// even when WiFi associated correctly.
func decodeIWSSIDEscape(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	b := make([]byte, 0, len(s))
	for i := 0; i < len(s); {
		if s[i] == '\\' && i+1 < len(s) {
			if s[i+1] == 'x' && i+3 < len(s) {
				if v, err := strconv.ParseUint(s[i+2:i+4], 16, 8); err == nil {
					b = append(b, byte(v))
					i += 4
					continue
				}
			}
			if s[i+1] == ' ' {
				b = append(b, ' ')
				i += 2
				continue
			}
		}
		b = append(b, s[i])
		i++
	}
	return string(b)
}

func parseIWScan(out string) []domain.Network {
	var list []domain.Network
	var current struct {
		bssid   string
		ssid    string
		signal  int
		channel int
	}
	lines := strings.Split(out, "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if m := reBSS.FindStringSubmatch(line); len(m) > 1 {
			if current.bssid != "" && current.ssid != "" {
				list = append(list, domain.Network{
					BSSID:    current.bssid,
					SSID:     current.ssid,
					Signal:   current.signal,
					Channel:  current.channel,
					Mode:     "STA",
					Rate:     "",
					Security: "",
				})
			}
			current.bssid = m[1]
			current.ssid = ""
			current.signal = 0
			current.channel = 0
			continue
		}
		if m := reSSID.FindStringSubmatch(line); len(m) > 1 {
			current.ssid = decodeIWSSIDEscape(strings.TrimSpace(m[1]))
			continue
		}
		if m := reSignal.FindStringSubmatch(line); len(m) > 1 {
			f, _ := strconv.ParseFloat(m[1], 64)
			current.signal = int(f)
			continue
		}
		if m := reDS.FindStringSubmatch(line); len(m) > 1 {
			current.channel, _ = strconv.Atoi(m[1])
			continue
		}
	}
	if current.bssid != "" && current.ssid != "" {
		list = append(list, domain.Network{
			BSSID:    current.bssid,
			SSID:     current.ssid,
			Signal:   current.signal,
			Channel:  current.channel,
			Mode:     "STA",
			Rate:     "",
			Security: "",
		})
	}
	return list
}

// GetCurrentIP returns the IPv4 address of the default interface (e.g. wlan0), or empty string if none.
func (s *Service) GetCurrentIP() (string, error) {
	cmd := exec.Command("ip", "-4", "addr", "show", defaultInterface)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("ip addr: %w", err)
	}
	if m := reInet.FindStringSubmatch(string(out)); len(m) > 1 {
		return m[1], nil
	}
	slog.Debug("no IP found", "component", "network", "output", string(out))
	return "", nil
}

// CurrentNetwork returns the currently connected network using iwgetid -r wlan0.
func (s *Service) CurrentNetwork() (*domain.Network, error) {
	ssid := ReadCurrentSSID()
	if ssid == "" {
		return nil, nil
	}
	signal, linkRate := readCurrentLink()
	return &domain.Network{
		SSID:     ssid,
		Mode:     "",
		BSSID:    "",
		Channel:  0,
		Rate:     "",
		Signal:   signal,
		LinkRate: linkRate,
		Security: "",
	}, nil
}

// readCurrentLink parses `iw dev <iface> link` for the associated AP's
// signal strength (dBm) and tx bitrate (Mbps, rounded). Returns (0, 0) when
// the interface is not associated or parsing fails — callers treat 0 as
// "unknown". Single shell-out keeps the two values consistent.
func readCurrentLink() (signal int, linkRate int) {
	out, err := exec.Command("iw", "dev", defaultInterface, "link").Output()
	if err != nil {
		return 0, 0
	}
	s := string(out)
	if m := reSignal.FindStringSubmatch(s); len(m) > 1 {
		f, _ := strconv.ParseFloat(m[1], 64)
		signal = int(f)
	}
	if m := reTxRate.FindStringSubmatch(s); len(m) > 1 {
		f, _ := strconv.ParseFloat(m[1], 64)
		linkRate = int(f + 0.5)
	}
	return signal, linkRate
}

// ReadCurrentSSID resolves the current SSID via a fallback chain — iwgetid
// alone has been observed to return empty on some Pi images even with an
// active connection (driver / utility version skew). Try the most direct
// tool first, then fall back to iw and wpa_cli so the polling loop in
// SetupNetwork can confirm the association without timing out. Exported so the
// system.info MQTT probe reuses the same chain instead of calling iwgetid alone.
func ReadCurrentSSID() string {
	if out, err := exec.Command("iwgetid", "-r", defaultInterface).Output(); err == nil {
		if s := strings.TrimSpace(string(out)); s != "" {
			return s
		}
	}
	// `iw dev <iface> link` lines like:
	//   Connected to aa:bb:...
	//   SSID: Glinks
	// Non-ASCII bytes come back as `\xNN` escapes — decode so the value
	// matches the raw UTF-8 the user typed (e.g. Chinese SSIDs).
	if out, err := exec.Command("iw", "dev", defaultInterface, "link").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "SSID:") {
				if s := strings.TrimSpace(strings.TrimPrefix(line, "SSID:")); s != "" {
					return decodeIWSSIDEscape(s)
				}
			}
		}
	}
	// `wpa_cli -i <iface> status` lines include `ssid=Glinks`. wpa_cli
	// uses the same `\xNN` escape format as iw for non-printable bytes.
	if out, err := exec.Command("wpa_cli", "-i", defaultInterface, "status").Output(); err == nil {
		for _, line := range strings.Split(string(out), "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "ssid=") {
				if s := strings.TrimSpace(strings.TrimPrefix(line, "ssid=")); s != "" {
					return decodeIWSSIDEscape(s)
				}
			}
		}
	}
	return ""
}

// CheckInternet pings 8.8.8.8. Unchanged.
func (s *Service) CheckInternet() (bool, error) {
	pingCmd := exec.Command("ping", "-c", "1", "-W", "5", "8.8.8.8")
	if err := pingCmd.Run(); err != nil {
		return false, fmt.Errorf("connected but no internet: ping 8.8.8.8 failed: %w", err)
	}
	return true, nil
}

// pingNetworkMonitor runs a short ping with networkMonitorPingTimeout. Used by network monitor only.
func (s *Service) pingNetworkMonitor(target string) bool {
	sec := int(networkMonitorPingTimeout.Seconds())
	if sec < 1 {
		sec = 1
	}
	cmd := exec.Command("ping", "-c", "1", "-W", strconv.Itoa(sec), target)
	return cmd.Run() == nil
}

// StartNetworkMonitor runs the network monitor loop in a goroutine. Call only when in STA mode (after setup).
// After networkMonitorFailsRequired consecutive failures, onLost is called (if non-nil).
// When internet is restored after a confirmed outage, onRestored is called (if non-nil).
// Exits when ctx is cancelled.
func (s *Service) StartNetworkMonitor(ctx context.Context, onLost, onRestored func()) {
	s.onConnectivityLost = onLost
	s.onConnectivityRestored = onRestored
	go func() {
		ticker := time.NewTicker(networkMonitorInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.runNetworkMonitorTick()
			}
		}
	}()
}

func (s *Service) runNetworkMonitorTick() {
	// Skip when setup not completed (e.g. factory reset, AP mode)
	if !s.config.SetUpCompleted {
		s.networkMonitorMu.Lock()
		s.networkMonitorConsecutive = 0
		s.networkMonitorMu.Unlock()
		return
	}
	if s.pingNetworkMonitor(networkMonitorPingTarget) {
		s.networkMonitorMu.Lock()
		prev := s.networkMonitorConsecutive
		s.networkMonitorConsecutive = 0
		s.reconnectAttempts = 0
		s.networkMonitorMu.Unlock()
		if prev >= networkMonitorFailsRequired {
			slog.Info("internet restored", "component", "network-monitor", "previousFails", prev)
			if s.onConnectivityRestored != nil {
				s.onConnectivityRestored()
			}
		}
		return
	}
	s.networkMonitorMu.Lock()
	s.networkMonitorConsecutive++
	n := s.networkMonitorConsecutive
	s.networkMonitorMu.Unlock()

	slog.Warn("no internet", "component", "network-monitor", "target", networkMonitorPingTarget, "fails", n, "required", networkMonitorFailsRequired)
	if n == networkMonitorFailsRequired && s.onConnectivityLost != nil {
		s.onConnectivityLost()
	}

	// Auto-reconnect: restart wlan0 after sustained outage
	if n >= networkMonitorReconnectAt && time.Since(s.lastReconnectAttempt) >= networkMonitorReconnectCooldown {
		s.lastReconnectAttempt = time.Now()
		go s.reconnectWiFi()
	}
}

// reconnectWiFi restarts wpa_supplicant and wlan0 to recover from WiFi drops.
// After networkMonitorMaxReconnects failed attempts, reboots the device.
func (s *Service) reconnectWiFi() {
	s.networkMonitorMu.Lock()
	s.reconnectAttempts++
	attempt := s.reconnectAttempts
	s.networkMonitorMu.Unlock()

	slog.Warn("attempting WiFi reconnect", "component", "network-monitor", "attempt", attempt)

	// Restart wpa_supplicant to re-associate with the AP
	_ = exec.Command("systemctl", "restart", "wpa_supplicant@wlan0").Run()
	time.Sleep(3 * time.Second)

	// Bounce the interface
	_ = exec.Command("ip", "link", "set", defaultInterface, "down").Run()
	time.Sleep(2 * time.Second)
	_ = exec.Command("ip", "link", "set", defaultInterface, "up").Run()
	time.Sleep(5 * time.Second)

	if s.pingNetworkMonitor(networkMonitorPingTarget) {
		slog.Info("WiFi reconnect succeeded", "component", "network-monitor", "attempt", attempt)
		s.networkMonitorMu.Lock()
		s.reconnectAttempts = 0
		s.networkMonitorMu.Unlock()
		return
	}

	slog.Warn("WiFi reconnect failed", "component", "network-monitor", "attempt", attempt, "max", networkMonitorMaxReconnects)

	if attempt >= networkMonitorMaxReconnects {
		slog.Error("WiFi reconnect exhausted — rebooting device", "component", "network-monitor", "attempts", attempt)
		_ = exec.Command("sudo", "reboot").Run()
	}
}

// ResetNetwork resets the network to the default state (clears credentials and writes minimal
// wpa_supplicant config). Restarts wpa_supplicant so it reloads the empty config and disconnects;
// if already in AP mode (wpa_supplicant masked), restart may fail and is ignored.
func (s *Service) ResetNetwork() error {
	s.config.NetworkSSID = ""
	s.config.NetworkPassword = ""
	wpaSupplicantConf := "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
	_ = os.Remove(wpaSupplicantConf)
	minimal := "ctrl_interface=DIR=/run/wpa_supplicant\nupdate_config=1\ncountry=US\nfast_reauth=1\nap_scan=1"
	_ = os.WriteFile(wpaSupplicantConf, []byte(minimal), 0600)
	// Restart wpa_supplicant so it reloads the config and disconnects from WiFi.
	// Ignore error: when in AP mode, wpa_supplicant is masked and restart fails.
	_ = exec.Command("systemctl", "restart", "wpa_supplicant@wlan0").Run()
	return s.config.Save()
}

// SetupNetwork submits WiFi credentials via connect-wifi CLI.
func (s *Service) SetupNetwork(ssid string, password string) (bool, error) {
	ssid = strings.TrimSpace(ssid)
	slog.Debug("starting network setup", "component", "network", "ssid", ssid)
	if ssid == "" {
		return false, fmt.Errorf("ssid is required")
	}
	// 802.11 caps SSID at 32 bytes. Counted in bytes, not chars — 1 Chinese
	// UTF-8 char = 3 bytes, so an SSID that "looks" short can still overflow.
	// Without this check, wpa_supplicant silently rejects the config and the
	// 60s polling loop returns a generic "no internet or SSID did not match"
	// error that's nearly impossible to debug from the web UI.
	if n := len(ssid); n > 32 {
		return false, fmt.Errorf("ssid too long: %d bytes, max 32 (802.11 limit)", n)
	}

	// Fast path: re-running setup with the SAME ssid+password we're already
	// connected to. connect-wifi rewrites wpa_supplicant.conf and restarts
	// the service even when the config wouldn't change, which costs a 6-10s
	// disconnect window and floods the polling loop with "does not match"
	// debug logs. Skip the disruption when nothing actually needs to change.
	if password == s.config.NetworkPassword {
		if cur, _ := s.CurrentNetwork(); cur != nil && cur.SSID == ssid {
			if ok, _ := s.CheckInternet(); ok {
				slog.Info("network setup: already connected to requested SSID, skipping reconnect", "component", "network", "ssid", ssid)
				s.config.NetworkSSID = ssid
				if err := s.config.Save(); err != nil {
					slog.Error("save config failed", "component", "network", "error", err)
				}
				return true, nil
			}
		}
	}

	args := []string{ssid}
	if password != "" {
		args = append(args, password)
	}
	slog.Debug("running connect-wifi", "component", "network", "args", args)
	cmd := exec.Command("connect-wifi", args...)
	slog.Debug("connect-wifi command", "component", "network", "cmd", cmd)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return false, fmt.Errorf("connect-wifi: %w: %s", err, string(out))
	}
	slog.Debug("connect-wifi output", "component", "network", "output", string(out))
	// Wait up to 60s for internet and matching SSID
	success := false
	for i := 0; i < 60; i++ {
		slog.Debug("checking internet", "component", "network", "attempt", i)
		// Check internet
		if ok, _ := s.CheckInternet(); ok {
			slog.Debug("internet ok", "component", "network", "attempt", i)
			// Check SSID
			curNet, _ := s.CurrentNetwork()
			slog.Debug("current network", "component", "network", "network", curNet)
			if curNet != nil && curNet.SSID == ssid {
				success = true
				break
			} else {
				current := ""
				if curNet != nil {
					current = curNet.SSID
				}
				slog.Debug("current network does not match", "component", "network", "current", current, "expected", ssid)
			}
		} else {
			slog.Debug("internet not ok", "component", "network", "attempt", i)
		}
		time.Sleep(1 * time.Second)
	}
	if !success {
		return false, fmt.Errorf("network setup failed, no internet or SSID did not match within 60s")
	}
	s.config.NetworkSSID = ssid
	s.config.NetworkPassword = password
	if err := s.config.Save(); err != nil {
		slog.Error("save config failed", "component", "network", "error", err)
	}
	slog.Info("network setup success", "component", "network")
	return true, nil
}

// SwitchToAPMode runs device-ap-mode to return to provisioning (AP) mode for reconfiguring WiFi.
func (s *Service) SwitchToAPMode() error {
	cmd := exec.Command("/usr/local/bin/device-ap-mode")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("device-ap-mode: %w: %s", err, string(out))
	}
	slog.Info("switched to AP mode", "component", "network")
	return nil
}
