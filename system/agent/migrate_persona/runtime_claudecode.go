package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// claudecodeAdapter reads/writes the Claude Code workspace layout
// (/root/.claudecode/workspace). The layout is IDENTICAL to OpenClaw's —
// SOUL.md, the owner's name in its own IDENTITY.md, dedicated KNOWLEDGE.md +
// daily memory/*.md slots, long-term MEMORY.md at the workspace root — because
// the OS-managed CLAUDE.md block @imports each file into Claude's context
// (runtimes/claudecode/onboarding.go), so every slot maps 1:1 and a round-trip
// with any slot-bearing runtime is structurally lossless. CLAUDE.md itself is
// NOT carried: it is the runtime's own loader file (OS-managed block + owner
// notes), the claudecode analog of AGENTS.md — runtime-specific, per
// docs/agentic/adding-agent-runtime.md §4.
type claudecodeAdapter struct{}

func (claudecodeAdapter) runtime() Runtime { return RuntimeClaudeCode }

func (claudecodeAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.ClaudecodeWorkspace
	soul, _ := os.ReadFile(filepath.Join(ws, "SOUL.md")) // missing → "" → writer skips

	b := &PersonaBundle{
		Soul:      string(soul),
		Identity:  readIdentityFields(filepath.Join(ws, "IDENTITY.md")),
		Memory:    parseEntries(filepath.Join(ws, "MEMORY.md")),
		Knowledge: parseEntries(filepath.Join(ws, "KNOWLEDGE.md")),
		User:      parseEntries(filepath.Join(ws, "USER.md")),
	}
	if opts.IncludeDailyMemory {
		for _, f := range dailyMemoryFiles(filepath.Join(ws, "memory")) {
			b.Daily = append(b.Daily, parseEntries(f)...)
		}
	}
	return b, nil
}

func (claudecodeAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.ClaudecodeWorkspace

	// Persona → SOUL.md. Strip any inlined identity card (Claude Code owns the
	// name via IDENTITY.md, like OpenClaw) and rebrand to Claude Code.
	m.writePersona("soul", rebrandToClaudeCode(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	// Identity → IDENTITY.md (its native slot; inverse of a runtime that inlined it).
	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToClaudeCode)

	// Long-term memory → MEMORY.md. Daily entries fold in here (date-stamped
	// daily files can't be faithfully reconstructed from entries).
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToClaudeCode),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// Distilled learnings → KNOWLEDGE.md (Claude Code has the slot). Only when
	// the source carried it; a source without the slot (Hermes) leaves this untouched.
	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToClaudeCode),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	// User profile → USER.md.
	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToClaudeCode),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reClaudeCode matches "Claude Code" / "ClaudeCode" / "claude-code" variants —
// used by the OpenClaw / Hermes / PicoClaw write adapters to rebrand a persona
// arriving FROM claudecode onto their name. Deliberately requires the "Code"
// word: a bare `\bClaude\b` would also rewrite legitimate mentions of the
// Claude model in memories.
var reClaudeCode = regexp.MustCompile(`(?i)\bClaude[\s-]?Code\b`)

// rebrandToClaudeCode — case-preserving. Rebrands OTHER runtimes' names
// (OpenClaw / Hermes / PicoClaw and the legacy ClawdBot/MoltBot aliases) onto
// Claude Code when a persona/memory arrives from them. Mirrors rebrandToHermes.
func rebrandToClaudeCode(text string) string {
	repl := casePreserving("Claude Code")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter — Claude Code mirrors the OpenClaw layout.
func (claudecodeAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.ClaudecodeWorkspace)
}

// userProfilePath implements runtimeAdapter — USER.md at the workspace root.
func (claudecodeAdapter) userProfilePath(opts Options) string {
	if opts.ClaudecodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.ClaudecodeWorkspace, "USER.md")
}
