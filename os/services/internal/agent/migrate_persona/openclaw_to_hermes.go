package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
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

// identityCardHeading marks the block soulToHermes appends; also the idempotency
// guard so a round-trip (hermes→openclaw→hermes) doesn't append it twice.
const identityCardHeading = "## Your identity card"

// identityReadInstruction is appended to the SOUL.md copied into the Hermes home.
// OpenClaw auto-injects IDENTITY.md into the agent's prompt; Hermes does not — it
// only loads SOUL.md — so the soul itself must tell the agent to open IDENTITY.md
// (Hermes has file-read tools). Without this the agent keeps its SOUL persona but
// loses the IDENTITY.md name/vibe the owner set.
const identityReadInstruction = "\n\n" + identityCardHeading + "\n\n" +
	"Your name and identity — name, vibe, emoji — live in `IDENTITY.md` in your home " +
	"directory. **Read `IDENTITY.md` at the start of each session.** It is who you " +
	"currently are; if it names you, that name wins over any default above.\n"

// soulToHermes rebrands the soul AND appends the identity-card read instruction,
// since Hermes (unlike OpenClaw) does not load IDENTITY.md into the prompt for you.
// Idempotent: skips the append if the soul already carries the block (e.g. it came
// back through a prior hermes→openclaw→hermes round-trip).
func soulToHermes(text string) string {
	text = rebrandToHermes(text)
	if strings.Contains(text, identityCardHeading) {
		return text
	}
	return text + identityReadInstruction
}

// openclawToHermes migrates persona + memory from an OpenClaw workspace into a
// Hermes home, following the upstream mapping:
//
//	workspace/SOUL.md      → <hermes>/SOUL.md                 (direct copy, rebranded)
//	workspace/IDENTITY.md  → <hermes>/IDENTITY.md            (direct copy, rebranded)
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

	// Persona: SOUL.md → <hermes>/SOUL.md (rebranded + identity-card read hint,
	// since Hermes won't auto-load IDENTITY.md the way OpenClaw does).
	m.copyPersona("soul",
		filepath.Join(ws, "SOUL.md"),
		filepath.Join(m.opts.HermesRoot, "SOUL.md"),
		soulToHermes)

	// Identity card: IDENTITY.md → <hermes>/IDENTITY.md (rebranded). Carries the
	// agent's name/vibe across so the persona's identity isn't lost on the switch
	// (OpenClaw stores name + vibe + avatar here, separate from SOUL.md).
	m.copyPersona("identity",
		filepath.Join(ws, "IDENTITY.md"),
		filepath.Join(m.opts.HermesRoot, "IDENTITY.md"),
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
