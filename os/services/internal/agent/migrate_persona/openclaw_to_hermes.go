package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
)

// reBrand - case-preserving
var (
	reOpenClaw = regexp.MustCompile(`(?i)\bOpen[\s-]?Claw\b`)
	reClawdBot = regexp.MustCompile(`(?i)\bClawdBot\b`)
	reMoltBot  = regexp.MustCompile(`(?i)\bMoltBot\b`)
)

func rebrandToHermes(text string) string {
	repl := casePreserving("Hermes")
	text = reOpenClaw.ReplaceAllStringFunc(text, repl)
	text = reClawdBot.ReplaceAllStringFunc(text, repl)
	text = reMoltBot.ReplaceAllStringFunc(text, repl)
	return text
}

// openclawToHermes migrates persona + memory from an OpenClaw workspace into a
// Hermes home, following the upstream mapping:
//
//	workspace/SOUL.md      → <hermes>/SOUL.md                 (direct copy, rebranded)
//	workspace/MEMORY.md    → <hermes>/memories/MEMORY.md      (entry-merge, deduped)
//	workspace/memory/*.md  → <hermes>/memories/MEMORY.md      (daily files folded in)
//	workspace/USER.md      → <hermes>/memories/USER.md        (entry-merge, deduped)
type openclawToHermes struct {
	*baseMigrator
}

func (m *openclawToHermes) Direction() Direction { return OpenclawToHermes }

func (m *openclawToHermes) Migrate() (*Report, error) {
	ws := m.opts.OpenclawWorkspace
	hermesMem := filepath.Join(m.opts.HermesRoot, "memories")

	// Persona: SOUL.md → <hermes>/SOUL.md (rebranded).
	m.copyPersona("soul",
		filepath.Join(ws, "SOUL.md"),
		filepath.Join(m.opts.HermesRoot, "SOUL.md"),
		rebrandToHermes)

	// Long-term memory: MEMORY.md (+ daily memory/*.md) → memories/MEMORY.md.
	memSources := []string{filepath.Join(ws, "MEMORY.md")}
	if m.opts.IncludeDailyMemory {
		memSources = append(memSources, dailyMemoryFiles(filepath.Join(ws, "memory"))...)
	}
	m.mergeMemory("memory", memSources,
		filepath.Join(ws, "MEMORY.md"),
		filepath.Join(hermesMem, "MEMORY.md"),
		m.opts.MemoryCharLimit, hermesFormat, rebrandToHermes)

	// User profile: USER.md → memories/USER.md.
	m.mergeMemory("user-profile",
		[]string{filepath.Join(ws, "USER.md")},
		filepath.Join(ws, "USER.md"),
		filepath.Join(hermesMem, "USER.md"),
		m.opts.UserCharLimit, hermesFormat, rebrandToHermes)

	return m.report(OpenclawToHermes), nil
}

// dailyMemoryFiles lists workspace/memory/*.md in sorted order so the merge is deterministic across runs.
func dailyMemoryFiles(dir string) []string {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range ents {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".md" {
			out = append(out, filepath.Join(dir, e.Name()))
		}
	}
	sort.Strings(out)
	return out
}
