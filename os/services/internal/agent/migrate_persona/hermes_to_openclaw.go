package migratepersona

import (
	"path/filepath"
	"regexp"
	"strings"
)

// reBrand - case-preserving
var reHermes = regexp.MustCompile(`(?i)\bHermes\b`)

func rebrandToOpenclaw(text string) string {
	return reHermes.ReplaceAllStringFunc(text, casePreserving("OpenClaw"))
}

// stripIdentityCard removes the "## Your identity card" block that the
// openclaw→hermes migration inlines into the Hermes SOUL.md. That block exists
// ONLY because Hermes has no separate IDENTITY.md slot — OpenClaw keeps the
// owner's name in its own workspace/IDENTITY.md, so carrying the card back into
// the OpenClaw SOUL would duplicate identity state in the wrong file. The card is
// always appended as the trailing section, so this drops from its heading to the
// end of the document.
func stripIdentityCard(text string) string {
	idx := strings.Index(text, identityCardHeading)
	if idx < 0 {
		return text
	}
	return strings.TrimRight(text[:idx], " \t\r\n") + "\n"
}

// hermesToOpenclaw migrates persona + memory from a Hermes home back into an
// OpenClaw workspace — the reverse of openclawToHermes:
//
//	<hermes>/SOUL.md             → workspace/SOUL.md            (direct copy, rebranded)
//	<hermes>/memories/MEMORY.md  → workspace/MEMORY.md          (entry-merge, deduped)
//	<hermes>/memories/USER.md    → workspace/USER.md            (entry-merge, deduped)
//
// Hermes has no daily-memory concept, so there is no equivalent of OpenClaw's
// workspace/memory/*.md to bring across. Destination memory files are written
// as markdown bullets (openclawFormat) since OpenClaw re-parses them as markdown.
type hermesToOpenclaw struct {
	*baseMigrator
}

func (m *hermesToOpenclaw) Direction() Direction { return HermesToOpenclaw }

func (m *hermesToOpenclaw) Migrate() (*Report, error) {
	ws := m.opts.OpenclawWorkspace
	hermesMem := filepath.Join(m.opts.HermesRoot, "memories")

	// Persona: <hermes>/SOUL.md → workspace/SOUL.md. Strip the Hermes-only
	// identity card first (OpenClaw owns the name via IDENTITY.md, not SOUL.md),
	// then rebrand back.
	m.copyPersona("soul",
		filepath.Join(m.opts.HermesRoot, "SOUL.md"),
		filepath.Join(ws, "SOUL.md"),
		func(s string) string { return rebrandToOpenclaw(stripIdentityCard(s)) })

	// Long-term memory: memories/MEMORY.md → workspace/MEMORY.md.
	memSrc := filepath.Join(hermesMem, "MEMORY.md")
	m.mergeMemory("memory",
		[]string{memSrc}, memSrc,
		filepath.Join(ws, "MEMORY.md"),
		m.opts.MemoryCharLimit, openclawFormat, rebrandToOpenclaw)

	// User profile: memories/USER.md → workspace/USER.md.
	userSrc := filepath.Join(hermesMem, "USER.md")
	m.mergeMemory("user-profile",
		[]string{userSrc}, userSrc,
		filepath.Join(ws, "USER.md"),
		m.opts.UserCharLimit, openclawFormat, rebrandToOpenclaw)

	return m.report(HermesToOpenclaw), nil
}
