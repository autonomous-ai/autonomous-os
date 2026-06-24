package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// openclawAdapter reads/writes the OpenClaw workspace layout. OpenClaw keeps the
// owner's name in its own IDENTITY.md (not SOUL), and has dedicated KNOWLEDGE.md
// + daily memory/*.md slots, so its read surfaces those as distinct bundle fields
// and its write restores them to their own files.
type openclawAdapter struct{}

func (openclawAdapter) runtime() Runtime { return RuntimeOpenclaw }

func (openclawAdapter) read(opts Options) (*PersonaBundle, error) {
	ws := opts.OpenclawWorkspace
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

func (openclawAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	ws := opts.OpenclawWorkspace

	// Persona → SOUL.md. Strip any identity card (OpenClaw owns the name via
	// IDENTITY.md, not SOUL) and rebrand to OpenClaw.
	m.writePersona("soul", rebrandToOpenclaw(stripIdentityCard(b.Soul)), filepath.Join(ws, "SOUL.md"))

	// Identity → IDENTITY.md (restore into the native slot; the inverse of a
	// runtime that inlined it into SOUL).
	m.writeIdentityFields("identity", b.Identity, filepath.Join(ws, "IDENTITY.md"), rebrandToOpenclaw)

	// Long-term memory → MEMORY.md. Daily entries fold in here: OpenClaw's daily
	// files are date-stamped and cannot be faithfully reconstructed from entries,
	// so they land in MEMORY rather than being dropped.
	mem := append(append([]string{}, b.Memory...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(mem, rebrandToOpenclaw),
		filepath.Join(ws, "MEMORY.md"), opts.MemoryCharLimit, openclawFormat)

	// Distilled learnings → KNOWLEDGE.md (OpenClaw has the slot). Only when the
	// source carried it; a source without the slot (Hermes) leaves this untouched.
	if len(b.Knowledge) > 0 {
		m.writeMemoryEntries("knowledge", rebrandEntries(b.Knowledge, rebrandToOpenclaw),
			filepath.Join(ws, "KNOWLEDGE.md"), opts.MemoryCharLimit, openclawFormat)
	}

	// User profile → USER.md.
	m.writeMemoryEntries("user-profile", rebrandEntries(b.User, rebrandToOpenclaw),
		filepath.Join(ws, "USER.md"), opts.UserCharLimit, openclawFormat)
	return nil
}

// dailyMemoryFiles lists workspace/memory/*.md in sorted order so the merge is
// deterministic across runs.
func dailyMemoryFiles(dir string) []string {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var files []string
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		files = append(files, filepath.Join(dir, e.Name()))
	}
	sort.Strings(files)
	return files
}

// Brand rewriting to OpenClaw — case-preserving. Rebrands the names of OTHER
// runtimes (Hermes today) onto OpenClaw when a persona/memory arrives from them.
var reHermes = regexp.MustCompile(`(?i)\bHermes\b`)

func rebrandToOpenclaw(text string) string {
	return reHermes.ReplaceAllStringFunc(text, casePreserving("OpenClaw"))
}
