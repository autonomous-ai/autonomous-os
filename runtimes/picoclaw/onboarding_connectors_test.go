package picoclaw

import (
	"strings"
	"testing"
)

// TestAgentsMDBlock_RoutesConnectorsSkill guards the connectors-routing rule in
// PicoClaw's AGENTS.md block. Without it the agent doesn't know the device's
// Gmail/Calendar/etc. credentials are already on disk (via the connectors skill)
// and falls back to raw shell (apt/mutt/send_email.py) — the same regression
// fixed for openclaw/hermes/claudecode/codex/opencode.
func TestAgentsMDBlock_RoutesConnectorsSkill(t *testing.T) {
	if !strings.Contains(agentsMDBlock, "`connectors` skill") {
		t.Fatalf("agentsMDBlock missing connectors-skill routing rule:\n%s", agentsMDBlock)
	}
	if !strings.Contains(agentsMDBlock, "configs/") {
		t.Fatalf("agentsMDBlock connectors rule must reference the on-disk credential path:\n%s", agentsMDBlock)
	}
	if !strings.Contains(strings.ToLower(agentsMDBlock), "never install") &&
		!strings.Contains(strings.ToLower(agentsMDBlock), "never write your own") {
		t.Fatalf("agentsMDBlock connectors rule must forbid installing/writing an alternative client:\n%s", agentsMDBlock)
	}
}
