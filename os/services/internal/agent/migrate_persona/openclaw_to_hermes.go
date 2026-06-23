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

// identityCardHeading marks the identity block inlined into the Hermes soul; it's
// also the idempotency guard so a round-trip (hermes→openclaw→hermes) doesn't
// inline it twice.
const identityCardHeading = "## Your identity card"

// identityFieldRe matches a FILLED OpenClaw IDENTITY.md field line, e.g.
// "- **Name:** Ngân". Unfilled fields keep the placeholder on the next line, so a
// same-line value requirement naturally skips them.
var identityFieldRe = regexp.MustCompile(`^- \*\*(.+?):\*\*\s*(\S.*)$`)

// buildIdentityBlock reads OpenClaw's IDENTITY.md and renders its filled fields
// (Name / Vibe / Emoji the owner set) as a SOUL.md section. Hermes loads SOUL.md
// as the agent's identity but has NO slot for a separate IDENTITY.md (its own
// claw-migrate archives that file), so inlining into SOUL is the only reliable way
// to keep the owner's custom name under Hermes instead of falling back to SOUL's
// default. Returns "" when the file is absent or has no filled fields, leaving the
// soul unchanged.
func buildIdentityBlock(identityPath string) string {
	raw, err := os.ReadFile(identityPath)
	if err != nil {
		return ""
	}
	var fields []string
	for _, line := range strings.Split(string(raw), "\n") {
		mt := identityFieldRe.FindStringSubmatch(strings.TrimSpace(line))
		if mt == nil {
			continue
		}
		val := strings.TrimSpace(mt[2])
		if val == "" || strings.HasPrefix(val, "_(") { // unfilled placeholder
			continue
		}
		fields = append(fields, "- **"+mt[1]+":** "+val)
	}
	if len(fields) == 0 {
		return ""
	}
	body := rebrandToHermes(strings.Join(fields, "\n"))
	return "\n\n" + identityCardHeading + "\n\n" +
		"Your owner set this — it overrides any default name or vibe above.\n\n" +
		body + "\n"
}

// ensureIdentityInlined appends block to the Hermes SOUL.md when it isn't already
// present — idempotent and independent of copyPersona's Overwrite rules, so the
// inline lands even though `hermes claw migrate` (install.sh) usually writes
// SOUL.md first (which makes the copyPersona above conflict-skip). No-op when block
// is empty (no filled IDENTITY fields) or SOUL.md is absent.
func (m *openclawToHermes) ensureIdentityInlined(soulPath, block string) {
	const kind = "identity-inline"
	if block == "" {
		m.record(kind, "", soulPath, StatusSkipped, "no filled IDENTITY.md fields", nil)
		return
	}
	raw, err := os.ReadFile(soulPath)
	if err != nil {
		m.record(kind, "", soulPath, StatusSkipped, "soul not present to inline into", nil)
		return
	}
	if strings.Contains(string(raw), identityCardHeading) {
		m.record(kind, "", soulPath, StatusSkipped, "identity already inlined", nil)
		return
	}
	if !m.opts.Execute {
		m.record(kind, "", soulPath, StatusMigrated, "would inline identity", nil)
		return
	}
	details := map[string]any{}
	if bak, berr := m.backup(soulPath); berr != nil {
		m.record(kind, "", soulPath, StatusError, "backup failed: "+berr.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	updated := strings.TrimRight(string(raw), "\n") + block
	if werr := os.WriteFile(soulPath, []byte(updated), 0o644); werr != nil {
		m.record(kind, "", soulPath, StatusError, "write: "+werr.Error(), nil)
		return
	}
	m.record(kind, "", soulPath, StatusMigrated, "identity inlined into soul", details)
}

// openclawToHermes migrates persona + memory from an OpenClaw workspace into a
// Hermes home, following the upstream mapping:
//
//	workspace/SOUL.md      → <hermes>/SOUL.md                 (rebranded; IDENTITY.md
//	                         fields inlined — Hermes has no separate IDENTITY slot)
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
	soulDest := filepath.Join(m.opts.HermesRoot, "SOUL.md")
	m.copyPersona("soul",
		filepath.Join(ws, "SOUL.md"),
		soulDest,
		rebrandToHermes)

	// Inline the owner's IDENTITY.md fields (name/vibe) INTO the Hermes soul:
	// Hermes reads SOUL.md as its identity and has no slot for a standalone
	// IDENTITY.md. This runs as a separate idempotent append — NOT through the copy
	// above — because `hermes claw migrate` (install.sh) usually writes SOUL.md
	// first, so the copyPersona conflict-skips (Overwrite=false) and a transform
	// there would never land. ensureIdentityInlined appends to whatever SOUL.md
	// exists, so the custom name (e.g. "Ngân") survives the switch regardless.
	m.ensureIdentityInlined(soulDest, buildIdentityBlock(filepath.Join(ws, "IDENTITY.md")))

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
