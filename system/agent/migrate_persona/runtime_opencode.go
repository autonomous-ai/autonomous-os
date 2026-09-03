package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// opencodeAdapter reads/writes the OpenCode workspace layout
// (/root/.opencode/workspace). It is identical to OpenClaw's — SOUL.md, the
// owner's name in its own IDENTITY.md, MEMORY.md at the workspace root,
// dedicated KNOWLEDGE.md + daily memory/*.md slots — because presync.sh §1
// seeds the OpenCode workspace as a verbatim copy of OpenClaw's (and opencode
// reads AGENTS.md natively, so no prompt translation is needed either). Every
// slot maps 1:1, so a round-trip with any slot-bearing runtime is structurally
// lossless.
//
// Registering this adapter is what gives OpenCode two-way Go persona migration
// (opencode↔openclaw, opencode↔hermes, opencode↔picoclaw, opencode↔codex,
// opencode↔claudecode) — switching AWAY from opencode carries
// SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE back. The INBOUND openclaw→opencode
// direction overlaps with presync §1's one-shot copy (which additionally
// carries skills — Go migration does not); the two agree (same source) and the
// marker keeps presync from re-copying on later switches.
type opencodeAdapter struct{}

func (opencodeAdapter) runtime() Runtime { return RuntimeOpenCode }

func (opencodeAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.OpenCodeWorkspace
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

func (opencodeAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.OpenCodeWorkspace

	// Persona → SOUL.md. Strip any inlined identity card (OpenCode owns the name
	// via IDENTITY.md, like OpenClaw) and rebrand to OpenCode.
	m.writePersona("soul", rebrandToOpenCode(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	// Identity → IDENTITY.md (its native slot; inverse of a runtime that
	// inlined it into SOUL).
	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToOpenCode)

	// Long-term memory → MEMORY.md (workspace root, OpenClaw layout). Daily
	// entries fold in here — date-stamped daily files can't be faithfully
	// reconstructed from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToOpenCode),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// Distilled learnings → KNOWLEDGE.md. Only when the source carried it; a
	// source without the slot (Hermes) leaves this untouched.
	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToOpenCode),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	// User profile → USER.md.
	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToOpenCode),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reOpenCode matches OpenCode / "Open Code" / "open-code" variants — used by
// the other write adapters to rebrand a persona arriving FROM opencode onto
// their name. Requires the "Code" word so it never collides with OpenClaw.
var reOpenCode = regexp.MustCompile(`(?i)\bOpen[\s-]?Code\b`)

// rebrandToOpenCode — case-preserving. Rebrands OTHER runtimes' names (OpenClaw
// / Hermes / PicoClaw and the legacy ClawdBot/MoltBot aliases) onto OpenCode
// when a persona/memory arrives from them. Mirrors rebrandToCodex.
func rebrandToOpenCode(text string) string {
	repl := casePreserving("OpenCode")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter — OpenCode mirrors the OpenClaw layout.
func (opencodeAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.OpenCodeWorkspace)
}

// userProfilePath implements runtimeAdapter — USER.md at the workspace root.
func (opencodeAdapter) userProfilePath(opts Options) string {
	if opts.OpenCodeWorkspace == "" {
		return ""
	}
	return filepath.Join(opts.OpenCodeWorkspace, "USER.md")
}
