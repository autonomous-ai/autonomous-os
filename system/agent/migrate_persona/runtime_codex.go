package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
)

// codexAdapter reads/writes the Codex workspace layout. It is identical to
// OpenClaw's — SOUL.md, the owner's name in its own IDENTITY.md, MEMORY.md at
// the workspace root, dedicated KNOWLEDGE.md + daily memory/*.md slots —
// because presync.sh §1 seeds the Codex workspace as a verbatim copy of
// OpenClaw's (and Codex reads AGENTS.md natively, so no prompt translation is
// needed either).
//
// Registering this adapter is what gives Codex two-way Go persona migration
// (codex↔openclaw, codex↔hermes, codex↔picoclaw) — switching AWAY from codex
// carries SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE back. The INBOUND
// openclaw→codex direction overlaps with presync §1's one-shot copy (which
// additionally carries skills — Go migration does not); the two agree (same
// source) and the marker keeps presync from re-copying on later switches.
type codexAdapter struct{}

func (codexAdapter) runtime() Runtime { return RuntimeCodex }

func (codexAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.CodexWorkspace
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

func (codexAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.CodexWorkspace

	// Persona → SOUL.md. Strip any inlined identity card (Codex owns the name
	// via IDENTITY.md, like OpenClaw) and rebrand to Codex.
	m.writePersona("soul", rebrandToCodex(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	// Identity → IDENTITY.md (its native slot; inverse of a runtime that
	// inlined it into SOUL).
	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToCodex)

	// Long-term memory → MEMORY.md (workspace root, OpenClaw layout). Daily
	// entries fold in here — date-stamped daily files can't be faithfully
	// reconstructed from entries.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToCodex),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// Distilled learnings → KNOWLEDGE.md. Only when the source carried it; a
	// source without the slot (Hermes) leaves this untouched.
	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToCodex),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	// User profile → USER.md.
	m.writeUserProfile("user-profile", rebrandEntries(b.User, rebrandToCodex),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// reCodex matches Codex — used by the other write adapters to rebrand a
// persona arriving FROM codex onto their name.
var reCodex = regexp.MustCompile(`(?i)\bCodex\b`)

// rebrandToCodex — case-preserving. Rebrands OTHER runtimes' names (OpenClaw /
// Hermes / PicoClaw and the legacy ClawdBot/MoltBot aliases) onto Codex when a
// persona/memory arrives from them. Mirrors rebrandToPicoclaw.
func rebrandToCodex(text string) string {
	repl := casePreserving("Codex")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}

// personaPaths implements runtimeAdapter — Codex mirrors the OpenClaw layout.
func (codexAdapter) personaPaths(opts Options) []string {
	return openclawLayoutPersonaPaths(opts.CodexWorkspace)
}
