package mqtthandler

import (
	"bytes"
	"context"
	"log/slog"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
	"go.autonomous.ai/os/internal/network"
	"go.autonomous.ai/os/lib/core/system"
	"go.autonomous.ai/os/lib/hal"
	agenthttp "go.autonomous.ai/os/server/agent/delivery/http"
	"go.autonomous.ai/os/server/config"
)

// System info probes. All bounded by short timeouts — these are read-only
// introspection calls that the web dashboard polls; the broker callback must
// not block on a hung subprocess.
const (
	sysProbeTimeout = 2 * time.Second
	bootstrapBinary = "/usr/local/bin/bootstrap-server"
	sysInterface    = "wlan0"
)

// handleSystemInfo returns the full aggregate snapshot — versions + network +
// host — in a single MQTT response. Synchronous (no `starting` intermediate);
// all probes run inline and individual failures fall back to zero-value fields.
func (h *DeviceMQTTHandler) handleSystemInfo(env domain.MQTTDataCommand) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	data := domain.MQTTSystemInfoData{
		Versions: probeVersions(ctx),
		Network:  probeNetwork(ctx),
		Host:     h.probeHost(),
	}
	slog.Info("system.info", "component", "mqtt",
		"os-server", data.Versions.OSServer, "bootstrap", data.Versions.Bootstrap,
		"hal", data.Versions.Hal, "openclaw", data.Versions.OpenClaw,
		"ip", data.Network.PrivateIP, "ssid", data.Network.SSID)
	return h.publishDataResult(env.Kind, "success", "", data)
}

// handleSystemVersion returns only the versions block. Cheaper than system.info
// — skips network/host probing.
func (h *DeviceMQTTHandler) handleSystemVersion(env domain.MQTTDataCommand) error {
	ctx, cancel := context.WithTimeout(context.Background(), sysProbeTimeout*3)
	defer cancel()

	v := probeVersions(ctx)
	slog.Info("system.version", "component", "mqtt",
		"os-server", v.OSServer, "bootstrap", v.Bootstrap, "hal", v.Hal, "openclaw", v.OpenClaw)
	return h.publishDataResult(env.Kind, "success", "", v)
}

// handleSystemNetwork returns only the network block.
func (h *DeviceMQTTHandler) handleSystemNetwork(env domain.MQTTDataCommand) error {
	ctx, cancel := context.WithTimeout(context.Background(), sysProbeTimeout*2)
	defer cancel()

	n := probeNetwork(ctx)
	slog.Info("system.network", "component", "mqtt",
		"ip", n.PrivateIP, "mac", n.MAC, "ssid", n.SSID, "gw", n.Gateway)
	return h.publishDataResult(env.Kind, "success", "", n)
}

// probeVersions collects os-server + bootstrap + hal + openclaw versions.
//   - os-server: read straight from the ldflags-injected build var (no exec needed).
//   - bootstrap: exec `bootstrap-server --version`; "" on any error.
//   - hal: queried over HTTP from the local HAL service /version endpoint.
//   - openclaw: read from the cached version probed by the agent monitor.
//     OpenClawDetected distinguishes "not installed" from "installed but
//     unparseable".
func probeVersions(ctx context.Context) domain.MQTTVersionsData {
	out := domain.MQTTVersionsData{
		OSServer: config.OSVersion,
	}

	if v, err := system.Run(ctx, bootstrapBinary, "--version"); err != nil {
		slog.Warn("system.info: bootstrap --version failed", "component", "mqtt", "error", err)
	} else {
		out.Bootstrap = strings.TrimSpace(string(v))
	}

	if v, err := hal.GetVersion(); err != nil {
		slog.Warn("system.info: hal version failed", "component", "mqtt", "error", err)
	} else {
		out.Hal = strings.TrimSpace(v)
	}

	if v := agenthttp.GetOpenClawVersion(); v != "" {
		out.OpenClaw = v
		out.OpenClawDetected = true
	}

	return out
}

// probeNetwork collects wlan0 IPv4 + hardware MAC + current SSID + default
// gateway. Each piece probes independently — a missing SSID (AP mode) doesn't
// poison the IP/MAC fields.
func probeNetwork(ctx context.Context) domain.MQTTNetworkData {
	out := domain.MQTTNetworkData{Interface: sysInterface}

	if iface, err := net.InterfaceByName(sysInterface); err == nil {
		out.MAC = iface.HardwareAddr.String()
		if addrs, err := iface.Addrs(); err == nil {
			for _, a := range addrs {
				if ipnet, ok := a.(*net.IPNet); ok {
					if ip4 := ipnet.IP.To4(); ip4 != nil {
						out.PrivateIP = ip4.String()
						break
					}
				}
			}
		}
	}

	// SSID via the shared fallback chain (iwgetid → iw → wpa_cli); iwgetid
	// alone returns empty on some Pi images even while associated, which left
	// this field blank in system.info while the HTTP monitor (same chain)
	// showed it.
	out.SSID = network.ReadCurrentSSID()

	// `ip route show default` → "default via 192.168.1.1 dev wlan0 …".
	// Pull the gateway IPv4 out of the via-field; the rest of the line varies.
	if v, err := system.Run(ctx, "ip", "route", "show", "default"); err == nil {
		fields := strings.Fields(string(v))
		for i, f := range fields {
			if f == "via" && i+1 < len(fields) {
				out.Gateway = fields[i+1]
				break
			}
		}
	}

	return out
}

// probeHost collects hostname + uptime. DeviceID/Name come from in-process
// state so no exec needed.
func (h *DeviceMQTTHandler) probeHost() domain.MQTTHostData {
	out := domain.MQTTHostData{
		DeviceID:   h.config.DeviceID,
		DeviceName: device.GetDeviceMac(),
	}
	if name, err := os.Hostname(); err == nil {
		out.Hostname = name
	}
	out.UptimeSeconds = readUptimeSeconds()
	return out
}

// readUptimeSeconds parses /proc/uptime. First field = seconds since boot.
// Returns 0 on any read/parse error — the dashboard treats 0 as "unknown".
func readUptimeSeconds() int64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := bytes.Fields(data)
	if len(fields) == 0 {
		return 0
	}
	secs, err := strconv.ParseFloat(string(fields[0]), 64)
	if err != nil {
		return 0
	}
	return int64(secs)
}
