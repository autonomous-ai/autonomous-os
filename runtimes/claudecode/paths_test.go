package claudecode

import (
	"os"
	"strings"
	"testing"
)

// The package resolves its paths once at init from OS_AGENT_HOME /
// CLAUDECODE_HOME, so which of these two runs is a property of the process
// environment, not of the test. Both are exercised by `make os-test` (default
// env) and by an off-device run with OS_AGENT_HOME set.

// TestDevicePathDefaults pins the board layout: unset env must reproduce the
// hardcoded /root paths this backend shipped with, byte for byte.
func TestDevicePathDefaults(t *testing.T) {
	if os.Getenv("OS_AGENT_HOME") != "" || os.Getenv("CLAUDECODE_HOME") != "" {
		t.Skip("relocated run — see TestPathsFollowAgentHome")
	}
	for _, tc := range []struct{ name, got, want string }{
		{"WSURL", WSURL, "ws://127.0.0.1:18791/claude/ws/"},
		{"Token", Token, "autonomous_claudecode_token"},
		{"claudecodeHome", claudecodeHome, "/root/.claudecode"},
		{"EnvFile", EnvFile, "/root/.claudecode/.env"},
		{"claudecodeWorkspaceDir", claudecodeWorkspaceDir, "/root/.claudecode/workspace"},
		{"claudeUserDir", claudeUserDir, "/root/.claude"},
		{"claudeUserMDPath", claudeUserMDPath, "/root/.claude/CLAUDE.md"},
		{"claudecodeSkillsDir", claudecodeSkillsDir, "/root/.claude/skills"},
		{"claudeCredentialsPath", claudeCredentialsPath, "/root/.claude/.credentials.json"},
		{"claudeProjectsDirDefault", claudeProjectsDirDefault, "/root/.claude/projects"},
		{"deviceMainWorkspace", deviceMainWorkspace, "/root/.claudecode/workspace"},
		{"telegramTargetsFile", telegramTargetsFile, "/root/.lumi/telegram_targets.json"},
		{"telegramOffsetFile", telegramOffsetFile, "/root/.claudecode/telegram_offset.json"},
		{"codingSelFileDefault", codingSelFileDefault, "/root/.claudecode/telegram_coding.json"},
		{"normalizeFolder(~)", normalizeFolder("~"), "/root"},
		{"normalizeFolder(~/myapp)", normalizeFolder("~/myapp"), "/root/myapp"},
		{"normalizeFolder(myapp)", normalizeFolder("myapp"), "/root/myapp"},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}
	// The OS block a device writes must be the template verbatim — no rewrite.
	if claudeMDBlock != claudeMDBlockTemplate {
		t.Error("claudeMDBlock was rewritten on a device-default run")
	}
	if presyncStateFiles[1] != "/root/.claude/channels/telegram/.env" {
		t.Errorf("presyncStateFiles = %v", presyncStateFiles)
	}
}

// TestPathsFollowAgentHome is the off-device half: every path — including the
// absolute ones baked into the CLAUDE.md block — must move with OS_AGENT_HOME,
// or the device's Claude Code state lands in the developer's own ~/.claude.
func TestPathsFollowAgentHome(t *testing.T) {
	home := os.Getenv("OS_AGENT_HOME")
	if home == "" {
		t.Skip("device-default run — see TestDevicePathDefaults")
	}
	for _, tc := range []struct{ name, got, want string }{
		{"claudeUserDir", claudeUserDir, home + "/.claude"},
		{"claudecodeSkillsDir", claudecodeSkillsDir, home + "/.claude/skills"},
		{"claudeCredentialsPath", claudeCredentialsPath, home + "/.claude/.credentials.json"},
		{"claudeProjectsDirDefault", claudeProjectsDirDefault, home + "/.claude/projects"},
		{"telegramTargetsFile", telegramTargetsFile, home + "/.lumi/telegram_targets.json"},
		{"normalizeFolder(~)", normalizeFolder("~"), home},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}
	if strings.Contains(claudeMDBlock, "/root") {
		t.Errorf("claudeMDBlock still holds board paths:\n%s", claudeMDBlock)
	}
	if !strings.Contains(claudeMDBlock, "@"+claudecodeWorkspaceDir+"/SOUL.md") {
		t.Error("persona @import not rewritten")
	}
	if !strings.Contains(claudeMDBlock, claudecodeSkillsDir+"/connectors/SKILL.md") {
		t.Error("connectors skill path not rewritten")
	}
}
