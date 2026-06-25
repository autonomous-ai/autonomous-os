package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// picoclawAdapter reads/writes the PicoClaw workspace layout. It is identical to
// OpenClaw's — SOUL.md, the owner's name in its own IDENTITY.md, dedicated
// KNOWLEDGE.md + daily memory/*.md slots — with ONE difference: PicoClaw keeps the
// long-term MEMORY.md INSIDE memory/ (memory/MEMORY.md), not at the workspace root.
// So reads/writes target memory/MEMORY.md, and the daily-memory glob over memory/
// must exclude MEMORY.md.
//
// Registering this adapter is what gives PicoClaw two-way Go persona migration
// (picoclaw↔openclaw, picoclaw↔hermes) — switching AWAY from picoclaw now carries
// SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE back. Note the INBOUND direction
// (openclaw→picoclaw) is also handled by presync's `picoclaw migrate --workspace-only`
// (which additionally imports skills — Go migration does not); the Go reconciler then
// re-asserts persona on boot. The two overlap on inbound persona but agree (same
// source), and presync remains the only path that carries skills.
type picoclawAdapter struct{}

func (picoclawAdapter) runtime() Runtime { return RuntimePicoclaw }

func (picoclawAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.PicoclawWorkspace
	soul, _ := os.ReadFile(filepath.Join(ws, "SOUL.md")) // missing → "" → writer skips

	b := &PersonaBundle{
		Soul:      string(soul),
		Identity:  readIdentityFields(filepath.Join(ws, "IDENTITY.md")),
		Memory:    parseEntries(filepath.Join(ws, "memory", "MEMORY.md")),
		Knowledge: parseEntries(filepath.Join(ws, "KNOWLEDGE.md")),
		User:      parseEntries(filepath.Join(ws, "USER.md")),
	}
	if opts.IncludeDailyMemory {
		for _, f := range picoclawDailyMemoryFiles(filepath.Join(ws, "memory")) {
			b.Daily = append(b.Daily, parseEntries(f)...)
		}
	}
	return b, nil
}

func (picoclawAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.PicoclawWorkspace

	// Persona → SOUL.md. Strip any inlined identity card (PicoClaw owns the name via
	// IDENTITY.md, like OpenClaw) and rebrand to PicoClaw.
	m.writePersona("soul", rebrandToPicoclaw(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	// Identity → IDENTITY.md (its native slot; inverse of a runtime that inlined it).
	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToPicoclaw)

	// Long-term memory → memory/MEMORY.md (NOT workspace root). Daily entries fold in
	// here (date-stamped daily files can't be faithfully reconstructed from entries).
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToPicoclaw),
		filepath.Join(ws, "memory", "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// Distilled learnings → KNOWLEDGE.md (PicoClaw has the slot). Only when the source
	// carried it; a source without the slot (Hermes) leaves this untouched.
	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToPicoclaw),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	// User profile → USER.md.
	m.writeMemoryEntries("user-profile", rebrandEntries(b.User, rebrandToPicoclaw),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// picoclawDailyMemoryFiles lists workspace/memory/*.md in sorted order, EXCLUDING
// MEMORY.md (which shares that dir under picoclaw and is the long-term file, not a
// daily log — including it would double-count memory into Daily).
func picoclawDailyMemoryFiles(dir string) []string {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var files []string
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") || e.Name() == "MEMORY.md" {
			continue
		}
		files = append(files, filepath.Join(dir, e.Name()))
	}
	sort.Strings(files)
	return files
}

// rePicoClaw matches PicoClaw and spacing/casing variants — used by the OpenClaw and
// Hermes write adapters to rebrand a persona arriving FROM picoclaw onto their name.
var rePicoClaw = regexp.MustCompile(`(?i)\bPico[\s-]?Claw\b`)

// rebrandToPicoclaw — case-preserving. Rebrands OTHER runtimes' names (OpenClaw /
// Hermes and the legacy ClawdBot/MoltBot aliases) onto PicoClaw when a persona/memory
// arrives from them. Mirrors rebrandToHermes.
func rebrandToPicoclaw(text string) string {
	repl := casePreserving("PicoClaw")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reHermes.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}
