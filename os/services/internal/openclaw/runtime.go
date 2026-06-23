package openclaw

import (
	"context"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/lib/core/system"
)

// openclawVersionProbeTimeout caps the one-shot `openclaw --version` probe so a
// wedged CLI can't stall startup.
const openclawVersionProbeTimeout = 5 * time.Second

// openclawSemverRe captures the first semver-like token in `openclaw --version`
// output (e.g. "OpenClaw 2026.6.9 (c645ec4)" → "2026.6.9"). Mirrors the regex
// used in bootstrap; duplicated to avoid pulling bootstrap into this package.
var openclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// openclawSemverNumRe pulls the numeric year.minor.patch out of an already
// normalized semver string (the cached version) for RuntimeInfo comparisons.
var openclawSemverNumRe = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)`)

// openClawVersion caches the normalized OpenClaw binary version (e.g. "2026.6.9"),
// populated once at startup by PopulateOpenClawVersion(). Single source of truth:
// the MQTT `info` message reports it AND channel-config writers parse it to pick
// runtime-compatible field shapes (see RuntimeInfo / currentOpenclawRuntime). Valid
// until the process restarts.
var openClawVersion atomic.Pointer[string]

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.6.9").
// Empty string if openclaw is not installed or the version hasn't been populated yet.
func GetOpenClawVersion() string {
	if v := openClawVersion.Load(); v != nil {
		return *v
	}
	return ""
}

// PopulateOpenClawVersion shells out to `openclaw --version` with a short timeout
// and stores the normalized semver in openClawVersion. Empty result when openclaw
// is not on PATH or the command fails — the Status endpoint then returns "" and the
// UI renders nothing for that field.
func PopulateOpenClawVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), openclawVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "openclaw", "--version")
	if err != nil {
		slog.Warn("read openclaw version failed (expected if not on openclaw backend)", "component", "openclaw-probe", "error", err)
		return
	}
	line := strings.TrimSpace(strings.TrimRight(string(out), "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	v := ""
	if loc := openclawSemverRe.FindStringSubmatch(line); len(loc) > 1 {
		v = loc[1]
	}
	openClawVersion.Store(&v)
}

// RuntimeInfo carries the installed openclaw runtime's parsed version. Pass it
// into channel-config helpers (applySlackChannelConfig, future helpers) so they
// can pick field shapes the runtime actually accepts — avoiding reliance on
// `openclaw doctor --fix`, which only migrates legacy → current and cannot
// downgrade a config that is "ahead" of the runtime.
//
// Detected=false means the version is unknown; helpers should treat that as
// "assume modern runtime" since 2026.5.x is the production target. AtLeast honours
// that by returning true for any threshold when Detected is false.
type RuntimeInfo struct {
	Year, Minor, Patch int
	Detected           bool
}

// AtLeast reports whether the detected runtime is ≥ year.minor.0. When Detected
// is false (version unknown), returns true so callers fall through to
// modern-runtime defaults instead of regressing every device on a transient probe
// miss.
func (r RuntimeInfo) AtLeast(year, minor int) bool {
	if !r.Detected {
		return true
	}
	if r.Year != year {
		return r.Year > year
	}
	return r.Minor >= minor
}

// currentOpenclawRuntime parses the cached OpenClaw version (the same value the
// MQTT `info` message reports) into a RuntimeInfo — no extra shell-out. Returns
// RuntimeInfo{Detected:false} when the version is unknown, which AtLeast treats as
// a modern runtime.
func currentOpenclawRuntime() RuntimeInfo {
	m := openclawSemverNumRe.FindStringSubmatch(GetOpenClawVersion())
	if len(m) < 4 {
		return RuntimeInfo{}
	}
	year, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	patch, _ := strconv.Atoi(m[3])
	return RuntimeInfo{Year: year, Minor: minor, Patch: patch, Detected: true}
}
