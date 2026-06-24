package hermes

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/lib/core/system"
)

// hermesVersionProbeTimeout caps the one-shot `hermes --version` probe so a
// wedged CLI can't stall startup.
const hermesVersionProbeTimeout = 5 * time.Second

// hermesSemverRe captures the first semver-like token in `hermes --version`
// output (e.g. "Hermes Agent v0.17.0 (2026.6.19)" → "0.17.0").
var hermesSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// hermesVersion caches the parsed Hermes CLI version. Single source of truth,
// mirroring openclaw.openClawVersion: the MQTT `info` message reports it next to
// openclaw_version, and the agent Status endpoint reads it via HermesService.Version()
// so the web shows the active backend's version. Populated once at startup by
// PopulateHermesVersion(); valid until the process restarts.
var hermesVersion atomic.Pointer[string]

// GetHermesVersion returns the cached Hermes CLI version (e.g. "0.17.0"). Empty
// when hermes is not installed or the version hasn't been populated yet.
func GetHermesVersion() string {
	if v := hermesVersion.Load(); v != nil {
		return *v
	}
	return ""
}

// PopulateHermesVersion shells out to `hermes --version` with a short timeout and
// caches the normalized semver. Empty result when hermes is not on PATH or the
// command fails — callers then report "" for that field.
func PopulateHermesVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), hermesVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "hermes", "--version")
	if err != nil {
		slog.Warn("read hermes version failed (expected if not on hermes backend)", "component", "hermes-probe", "error", err)
		return
	}
	line := strings.TrimSpace(string(out))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	v := ""
	if loc := hermesSemverRe.FindStringSubmatch(line); len(loc) > 1 {
		v = loc[1]
	}
	hermesVersion.Store(&v)
}

// Version satisfies domain.AgentGateway.Version(): the cached Hermes CLI version,
// or empty when undetected.
func (s *HermesService) Version() string {
	return GetHermesVersion()
}
