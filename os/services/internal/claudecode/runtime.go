package claudecode

import (
	"context"
	"log/slog"
	"regexp"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/lib/core/system"
)

const claudecodeVersionProbeTimeout = 5 * time.Second

// `claude --version` prints e.g. "2.1.83 (Claude Code)".
var claudecodeSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)
var claudecodeVersion atomic.Pointer[string]

func GetClaudeCodeVersion() string {
	if v := claudecodeVersion.Load(); v != nil {
		return *v
	}
	return ""
}

func PopulateClaudeCodeVersion() {
	ctx, cancel := context.WithTimeout(context.Background(), claudecodeVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "claude", "--version")
	if err != nil {
		slog.Warn("read claude version failed (expected if not on claudecode backend)", "component", "claudecode-probe", "error", err)
		return
	}
	v := ""
	if loc := claudecodeSemverRe.FindStringSubmatch(string(out)); len(loc) > 1 {
		v = loc[1]
	}
	claudecodeVersion.Store(&v)
}

// Version satisfies domain.AgentGateway.Version(): the cached Claude Code CLI
// version, or empty when undetected.
func (s *ClaudeCodeService) Version() string {
	return GetClaudeCodeVersion()
}
