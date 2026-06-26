package picoclaw

import (
	"context"
	"log/slog"
	"regexp"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/lib/core/system"
)

const picoclawVersionProbeTimeout = 5 * time.Second

var picoclawGoVersionRe = regexp.MustCompile(`(?i)\bgo\d+\.\d+\.\d+\b`)
var picoclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)
var picoclawVersion atomic.Pointer[string]

func GetPicoclawVersion() string {
	if v := picoclawVersion.Load(); v != nil {
		return *v
	}
	return ""
}

func PopulatePicoclawVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), picoclawVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "picoclaw", "version")
	if err != nil {
		slog.Warn("read picoclaw version failed (expected if not on picoclaw backend)", "component", "picoclaw-probe", "error", err)
		return
	}
	// Drop the Go toolchain version so "go1.25.11" is never matched as the release.
	cleaned := picoclawGoVersionRe.ReplaceAllString(string(out), "")
	v := ""
	if loc := picoclawSemverRe.FindStringSubmatch(cleaned); len(loc) > 1 {
		v = loc[1]
	}
	picoclawVersion.Store(&v)
}

// Version satisfies domain.AgentGateway.Version(): the cached PicoClaw CLI
// version, or empty when undetected.
func (s *PicoclawService) Version() string {
	return GetPicoclawVersion()
}
