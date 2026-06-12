package device

import (
	"os"
	"regexp"
	"strings"
)

// GetDeviceMac returns the hardware ID in <device_type>-XXXX format (last 4 chars
// of the Pi serial / eth MAC). Same logic as setup.sh. Empty string if not on Pi
// or serial unavailable.
func GetDeviceMac() string {
	serial := readSerial()
	if serial == "" {
		return ""
	}
	suffix := serial
	if len(serial) > 4 {
		suffix = serial[len(serial)-4:]
	}
	// Device-type-driven identity: <device_type>-<suffix>, lowercase — matches the
	// mDNS hostname / AP SSID set by setup.sh, and is the redirect target the web UI
	// derives from this field. DEVICE_TYPE is the immutable hardware identity (env,
	// baked into the systemd unit at provisioning). Env-only (config.json tier
	// omitted — the env is always set on a provisioned device). No "lamp"
	// fallback: without a device_type we can't form a valid id, so return empty.
	deviceType := strings.ToLower(os.Getenv("DEVICE_TYPE"))
	if deviceType == "" {
		return ""
	}
	return deviceType + "-" + strings.ToLower(suffix)
}

func readSerial() string {
	// Pi 5: device-tree
	if b, err := os.ReadFile("/proc/device-tree/serial-number"); err == nil {
		return strings.TrimSpace(strings.TrimRight(string(b), "\x00"))
	}
	// Pi 4: cpuinfo
	if b, err := os.ReadFile("/proc/cpuinfo"); err == nil {
		re := regexp.MustCompile(`(?m)^Serial\s*:\s*(\S+)`)
		if m := re.FindSubmatch(b); len(m) >= 2 {
			return strings.TrimSpace(string(m[1]))
		}
	}
	// Non-Pi boards (e.g. OrangePi 4 Pro with Allwinner T527): fall back to the
	// ethernet MAC. Colons stripped so the last-4-chars suffix logic produces a
	// stable hex tag. eth0 covers most Pi-style boards; end0 covers OrangePi's
	// kernel-predictable naming.
	for _, iface := range []string{"eth0", "end0"} {
		if b, err := os.ReadFile("/sys/class/net/" + iface + "/address"); err == nil {
			mac := strings.TrimSpace(string(b))
			if mac != "" && mac != "00:00:00:00:00:00" {
				return strings.ReplaceAll(mac, ":", "")
			}
		}
	}
	return ""
}
