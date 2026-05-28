package device

import (
	"os"
	"regexp"
	"strings"
)

// GetDeviceMac returns the hardware ID in Lamp-XXXX format (last 4 chars of Pi serial).
// Same logic as setup.sh. Empty string if not on Pi or serial unavailable.
func GetDeviceMac() string {
	serial := readSerial()
	if serial == "" {
		return ""
	}
	suffix := serial
	if len(serial) > 4 {
		suffix = serial[len(serial)-4:]
	}
	return "Lamp-" + suffix
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
