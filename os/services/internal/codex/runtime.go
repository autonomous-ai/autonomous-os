package codex

import (
	"context"
	"log/slog"
	"regexp"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/lib/core/system"
)

const codexVersionProbeTimeout = 5 * time.Second

// codexSemverRe extracts the release from `codex --version` output
// (e.g. "codex-cli 0.142.5" → "0.142.5").
var codexSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)
var codexVersion atomic.Pointer[string]

func GetCodexVersion() string {
	if v := codexVersion.Load(); v != nil {
		return *v
	}
	return ""
}

func PopulateCodexVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), codexVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "codex", "--version")
	if err != nil {
		slog.Warn("read codex version failed (expected if not on codex backend)", "component", "codex-probe", "error", err)
		return
	}
	v := ""
	if loc := codexSemverRe.FindStringSubmatch(string(out)); len(loc) > 1 {
		v = loc[1]
	}
	codexVersion.Store(&v)
}

// Version satisfies domain.AgentGateway.Version(): the cached Codex CLI
// version, or empty when undetected.
func (s *CodexService) Version() string {
	return GetCodexVersion()
}
