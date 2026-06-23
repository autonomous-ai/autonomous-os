package migratepersona

import (
	"os"
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
	hermesSoul := filepath.Join(m.opts.HermesRoot, "SOUL.md")
	m.copyPersona("soul",
		hermesSoul,
		filepath.Join(ws, "SOUL.md"),
		func(s string) string { return rebrandToOpenclaw(stripIdentityCard(s)) })

	// Identity: the card just stripped from the OpenClaw SOUL carries the owner's
	// name/vibe — without restoring it into workspace/IDENTITY.md (the file OpenClaw
	// reads for the agent name → wake words) the name set under Hermes is dropped on
	// switch-back. Reads the Hermes SOUL (still has the card) regardless of the strip
	// above, which only affects the destination copy.
	m.restoreIdentityCard(hermesSoul, filepath.Join(ws, "IDENTITY.md"))

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

// identityField is one "- **Name:** value" line lifted from a Hermes SOUL
// identity card on the way back to OpenClaw.
type identityField struct{ name, value string }

// identityCardFields extracts the filled "- **Field:** value" lines from the
// identity card block of a Hermes SOUL.md. Scoped to the card (from
// identityCardHeading to end of document) so unrelated bold-bullet lines in the
// soul body are never misread as identity. Mirrors buildIdentityBlock's filled-
// field rule (same regex; skips empty / `_(...)_` placeholders).
func identityCardFields(soul string) []identityField {
	idx := strings.Index(soul, identityCardHeading)
	if idx < 0 {
		return nil
	}
	var out []identityField
	for _, line := range strings.Split(soul[idx:], "\n") {
		mt := identityFieldRe.FindStringSubmatch(strings.TrimSpace(line))
		if mt == nil {
			continue
		}
		val := strings.TrimSpace(mt[2])
		if val == "" || strings.HasPrefix(val, "_(") { // unfilled placeholder
			continue
		}
		out = append(out, identityField{name: mt[1], value: val})
	}
	return out
}

// setIdentityField replaces the first "**field:**" line's value (preserving the
// line's bullet prefix and dropping a stale italic placeholder hint beneath it),
// or appends "- **field:** value" when no such line exists. Inverse counterpart
// to openclaw.rewriteIdentityName, generalized to any field; duplicated here
// because migratepersona deliberately does not import internal/openclaw.
func setIdentityField(content, field, value string) string {
	lines := strings.Split(content, "\n")
	needle := "**" + strings.ToLower(field) + ":**"
	for i, line := range lines {
		idx := strings.Index(strings.ToLower(line), needle)
		if idx < 0 {
			continue
		}
		lines[i] = line[:idx] + "**" + field + ":** " + value
		if i+1 < len(lines) && isItalicPlaceholderLine(lines[i+1]) {
			lines = append(lines[:i+1], lines[i+2:]...)
		}
		return strings.Join(lines, "\n")
	}
	prefix := content
	if prefix != "" && !strings.HasSuffix(prefix, "\n") {
		prefix += "\n"
	}
	return prefix + "- **" + field + ":** " + value + "\n"
}

// isItalicPlaceholderLine reports whether line is a markdown italic note wrapped
// in `_(...)_` or `*(...)*` — OpenClaw IDENTITY.md template hints left under an
// unfilled field, stale once the field is filled.
func isItalicPlaceholderLine(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// restoreIdentityCard is the inverse of buildIdentityBlock + ensureIdentityInlined
// (openclaw→hermes): it pulls the owner's identity fields back OUT of the Hermes
// SOUL identity card and writes them into the OpenClaw workspace IDENTITY.md — the
// file OpenClaw (not SOUL) owns and reads for the agent name (parseIdentityName →
// WatchIdentity → wake words). Without it the name set under Hermes is lost on
// switch-back, since stripIdentityCard removes the card from the OpenClaw SOUL and
// nothing else carries it.
//
// Fields are written by line replace-or-append, preserving any existing template
// (descriptions, other slots) when IDENTITY.md already exists. When the file is
// absent (first switch, before OpenClaw's own onboard creates it) a minimal file
// is created; OpenClaw's onboard then leaves the existing file (and our name)
// intact — the same assumption UpdateIdentityName (device.rename) already relies
// on, proven by rename persisting across onboards.
func (m *hermesToOpenclaw) restoreIdentityCard(hermesSoulPath, identityPath string) {
	const kind = "identity-restore"
	raw, err := os.ReadFile(hermesSoulPath)
	if err != nil {
		m.record(kind, hermesSoulPath, identityPath, StatusSkipped, "hermes soul not present", nil)
		return
	}
	fields := identityCardFields(string(raw))
	if len(fields) == 0 {
		m.record(kind, hermesSoulPath, identityPath, StatusSkipped, "no identity card to restore", nil)
		return
	}

	existing, rerr := os.ReadFile(identityPath)
	if rerr != nil && !os.IsNotExist(rerr) {
		m.record(kind, hermesSoulPath, identityPath, StatusError, "read identity: "+rerr.Error(), nil)
		return
	}
	updated := string(existing)
	for _, f := range fields {
		updated = setIdentityField(updated, f.name, rebrandToOpenclaw(f.value))
	}
	if updated == string(existing) {
		m.record(kind, hermesSoulPath, identityPath, StatusSkipped, "identity already current", nil)
		return
	}

	if !m.opts.Execute {
		m.record(kind, hermesSoulPath, identityPath, StatusMigrated, "would restore identity", nil)
		return
	}

	details := map[string]any{}
	if bak, berr := m.backup(identityPath); berr != nil {
		m.record(kind, hermesSoulPath, identityPath, StatusError, "backup failed: "+berr.Error(), nil)
		return
	} else if bak != "" {
		details["backup"] = bak
	}
	if err := os.MkdirAll(filepath.Dir(identityPath), 0o755); err != nil {
		m.record(kind, hermesSoulPath, identityPath, StatusError, "create dest dir: "+err.Error(), nil)
		return
	}
	if err := os.WriteFile(identityPath, []byte(updated), 0o644); err != nil {
		m.record(kind, hermesSoulPath, identityPath, StatusError, "write: "+err.Error(), nil)
		return
	}
	m.record(kind, hermesSoulPath, identityPath, StatusMigrated, "identity restored to IDENTITY.md", details)
}
