package device

import (
	"bufio"
	"bytes"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/server/config"
)

// zoneInfoDir is the system tzdata tree. A zone name is valid iff a regular file
// exists at <zoneInfoDir>/<name> (e.g. Asia/Ho_Chi_Minh). Used both to validate
// an incoming zone before applying it and to enumerate the picker list when
// `timedatectl list-timezones` is unavailable.
const zoneInfoDir = "/usr/share/zoneinfo"

// timezoneFile is the Debian-style plain-text zone name file. HAL's clock helpers
// (os/hal/clock.py) read it fresh on every call, so writing it is what makes a
// timezone change take effect on the device WITHOUT restarting HAL. `timedatectl`
// alone does NOT touch this file (it only updates /etc/localtime), so we always
// write it ourselves — see the timezone-runtime-clock note.
const timezoneFile = "/etc/timezone"

// localtimeFile is the symlink glibc resolves for local wall-clock. We point it
// at the chosen zone's tzdata file directly (rather than relying on timedatectl,
// which may be absent) so the change lands even on a minimal image.
const localtimeFile = "/etc/localtime"

// commonTimezones is the fallback picker list when neither `timedatectl
// list-timezones` nor a walk of /usr/share/zoneinfo yields anything (e.g. a
// stripped image). Kept short and ops-relevant rather than exhaustive.
var commonTimezones = []string{
	"UTC",
	"Asia/Ho_Chi_Minh",
	"Asia/Bangkok",
	"Asia/Singapore",
	"Asia/Shanghai",
	"Asia/Tokyo",
	"Asia/Seoul",
	"Asia/Kolkata",
	"Asia/Dubai",
	"Europe/London",
	"Europe/Paris",
	"Europe/Berlin",
	"Europe/Moscow",
	"America/New_York",
	"America/Chicago",
	"America/Denver",
	"America/Los_Angeles",
	"America/Sao_Paulo",
	"Australia/Sydney",
	"Pacific/Auckland",
}

// isValidTimezone reports whether name is a real IANA zone present in the system
// tzdata tree. Rejects empty, absolute, and traversal-y inputs before touching
// the filesystem so a crafted name can't escape zoneInfoDir.
func isValidTimezone(name string) bool {
	name = strings.TrimSpace(name)
	if name == "" || strings.HasPrefix(name, "/") || strings.Contains(name, "..") {
		return false
	}
	info, err := os.Stat(filepath.Join(zoneInfoDir, name))
	return err == nil && info.Mode().IsRegular()
}

// currentSystemTimezone returns the device's active zone name. /etc/timezone is
// the authoritative source (same file HAL reads); when it's missing we resolve
// the /etc/localtime symlink back to a zone name. Empty when neither resolves.
func currentSystemTimezone() string {
	if data, err := os.ReadFile(timezoneFile); err == nil {
		if tz := strings.TrimSpace(string(data)); tz != "" {
			return tz
		}
	}
	if target, err := os.Readlink(localtimeFile); err == nil {
		// /etc/localtime → /usr/share/zoneinfo/<zone> → strip the prefix.
		if idx := strings.Index(target, "zoneinfo/"); idx != -1 {
			return target[idx+len("zoneinfo/"):]
		}
	}
	return ""
}

// listSystemTimezones returns the selectable zone names, newest source first:
// `timedatectl list-timezones` (systemd's curated set), else a walk of the
// tzdata tree, else the built-in commonTimezones fallback. Always non-empty.
func listSystemTimezones() []string {
	if out, err := exec.Command("timedatectl", "list-timezones").Output(); err == nil {
		zones := parseLines(out)
		if len(zones) > 0 {
			return zones
		}
	}
	if zones := walkZoneInfo(); len(zones) > 0 {
		return zones
	}
	return commonTimezones
}

// parseLines splits command output into trimmed, non-empty lines.
func parseLines(out []byte) []string {
	var lines []string
	scanner := bufio.NewScanner(bytes.NewReader(out))
	for scanner.Scan() {
		if l := strings.TrimSpace(scanner.Text()); l != "" {
			lines = append(lines, l)
		}
	}
	return lines
}

// walkZoneInfo enumerates zone names from the tzdata tree. Only the standard
// Area/Location zones (a "/" in the relative path, e.g. Asia/Ho_Chi_Minh) plus
// UTC are returned, skipping the posix/right/ duplicate trees and bare legacy
// aliases so the picker mirrors what timedatectl would show.
func walkZoneInfo() []string {
	var zones []string
	_ = filepath.Walk(zoneInfoDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !info.Mode().IsRegular() {
			return nil
		}
		rel, relErr := filepath.Rel(zoneInfoDir, path)
		if relErr != nil {
			return nil
		}
		if strings.HasPrefix(rel, "posix/") || strings.HasPrefix(rel, "right/") {
			return nil
		}
		if rel == "UTC" || strings.Contains(rel, "/") {
			zones = append(zones, rel)
		}
		return nil
	})
	sort.Strings(zones)
	return zones
}

// applySystemTimezone points /etc/localtime at the zone's tzdata file and writes
// /etc/timezone, the two changes that actually move the device's wall-clock.
// `timedatectl set-timezone` is also invoked best-effort so systemd + a running
// timesyncd learn the new zone, but it is not required for correctness. Caller
// must have validated `tz` via isValidTimezone first.
func applySystemTimezone(tz string) error {
	// /etc/localtime → the chosen zone. Replace atomically (remove then symlink)
	// so a stale symlink can't linger if the target type changed.
	zonePath := filepath.Join(zoneInfoDir, tz)
	_ = os.Remove(localtimeFile)
	if err := os.Symlink(zonePath, localtimeFile); err != nil {
		return fmt.Errorf("link %s: %w", localtimeFile, err)
	}
	// /etc/timezone — the file HAL's clock helpers read. Trailing newline matches
	// the Debian convention (skills do `cat /etc/timezone`).
	if err := os.WriteFile(timezoneFile, []byte(tz+"\n"), 0644); err != nil {
		return fmt.Errorf("write %s: %w", timezoneFile, err)
	}
	// Best-effort: keep systemd's view in sync. Failure (binary absent on a
	// minimal image / dev box) is non-fatal — the two writes above already moved
	// the clock.
	if out, err := exec.Command("timedatectl", "set-timezone", tz).CombinedOutput(); err != nil {
		slog.Warn("timedatectl set-timezone failed (non-fatal)", "component", "device", "tz", tz, "error", err, "output", strings.TrimSpace(string(out)))
	}
	return nil
}

// GetTimezone returns the device's current zone plus the selectable list, for
// GET /api/device/timezone. Current prefers the live system value (config is just
// a record); when the system can't be read it falls back to config.Timezone.
func (s *Service) GetTimezone() (current string, zones []string) {
	current = currentSystemTimezone()
	if current == "" {
		current = s.config.Timezone
	}
	return current, listSystemTimezones()
}

// CurrentTimezone returns just the device's active IANA zone (live from the
// system, config.Timezone as fallback) — without enumerating the selectable
// list. Cheap enough to call on every MQTT info uplink, unlike GetTimezone which
// shells out to `timedatectl list-timezones`.
func (s *Service) CurrentTimezone() string {
	if tz := currentSystemTimezone(); tz != "" {
		return tz
	}
	return s.config.Timezone
}

// SetTimezone validates, applies (localtime + /etc/timezone + best-effort
// timedatectl), and persists the chosen IANA zone to config.json. HAL's clock
// helpers read /etc/timezone fresh per call, so the change takes effect without a
// HAL restart. Returns an error for an unknown zone (nothing is written).
func (s *Service) SetTimezone(tz string) error {
	tz = strings.TrimSpace(tz)
	if !isValidTimezone(tz) {
		return fmt.Errorf("unknown timezone %q", tz)
	}
	if err := applySystemTimezone(tz); err != nil {
		return fmt.Errorf("apply timezone: %w", err)
	}
	// Re-point THIS process's cached local zone. Go reads /etc/localtime once at
	// startup and caches time.Local for the process lifetime, so os-server's own
	// local-date logic (the daily JSONL buckets in flow/history/analytics and
	// lib/posture|music|mood|wellbeing, all keyed by time.Now().Format("2006-01-02"))
	// would keep using the OLD zone until restart. Updating time.Local here makes
	// the change live in-process, so changing the timezone needs NO os-server
	// restart — matching HAL, which reads /etc/timezone fresh per call.
	if loc, err := time.LoadLocation(tz); err == nil {
		time.Local = loc
	} else {
		slog.Warn("load location for in-process time.Local failed", "component", "device", "tz", tz, "error", err)
	}
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.Timezone = tz
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("timezone updated", "component", "device", "tz", tz)
	return nil
}
