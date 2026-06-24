package migratepersona

import (
	"os"
	"regexp"
	"strings"
)

// PersonaBundle is the runtime-neutral canonical form of a device's persona +
// long-term memory. It is the hub of the migration: every runtime has ONE read
// adapter (its on-disk layout → bundle) and ONE write adapter (bundle → its
// layout), so migrating between any two runtimes is read[from] → write[to].
// That keeps the file count LINEAR (2 per runtime) instead of the quadratic
// N×(N-1) a per-pair migrator would need — adding a runtime is one adapter file
// that immediately interoperates with every existing runtime, both directions.
//
// Fields a runtime lacks are simply nil on read and folded by the write adapter
// (e.g. Hermes has no KNOWLEDGE/daily slot, so its writer folds those into
// Memory). Whether a round-trip is structurally lossless is therefore a property
// of each runtime's slots, decided in its adapter — see docs/agentic/
// adding-agent-runtime.md §4.
type PersonaBundle struct {
	// Soul is the persona/character body, with any inlined identity card stripped
	// (identity travels in Identity). Brand tokens are left as-is; the write
	// adapter rebrands to its own runtime name.
	Soul string
	// Identity holds the owner's filled identity fields (Name, Vibe, …). On a
	// runtime with a dedicated IDENTITY.md it comes from that file; on one that
	// inlines into SOUL (Hermes) it is parsed back out of the card.
	Identity []IdentityField
	// Memory / User are long-term memory + user-profile entries (canonical,
	// untransformed; the writer rebrands + entry-merges into the destination).
	Memory []string
	User   []string
	// Knowledge / Daily are distilled learnings + per-day memory. Populated only
	// by runtimes that keep them as separate slots (OpenClaw); nil otherwise.
	Knowledge []string
	Daily     []string
}

// IdentityField is one "- **Name:** value" line of the owner's identity.
type IdentityField struct{ name, value string }

// identityCardHeading marks the identity block inlined into a SOUL.md by runtimes
// that have no separate IDENTITY.md slot (Hermes). It is also the idempotency
// guard so a round-trip does not inline twice, and the strip boundary on the way
// back out.
const identityCardHeading = "## Your identity card"

// identityFieldRe matches a FILLED identity field line, e.g. "- **Name:** Ngân".
// Unfilled template fields keep the placeholder on the next line, so a same-line
// value requirement naturally skips them.
var identityFieldRe = regexp.MustCompile(`^- \*\*(.+?):\*\*\s*(\S.*)$`)

// readIdentityFields parses the filled identity fields from an IDENTITY.md file
// (the slot OpenClaw owns). Empty / `_(…)_` placeholders are skipped. Returns nil
// when the file is absent or has no filled fields.
func readIdentityFields(path string) []IdentityField {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return parseIdentityLines(string(raw))
}

// identityCardFields extracts the filled identity fields from the "## Your
// identity card" block of a SOUL.md (the slot Hermes uses). Scoped to the card
// (heading → EOF) so unrelated bold-bullet lines in the soul body are never
// misread as identity.
func identityCardFields(soul string) []IdentityField {
	idx := strings.Index(soul, identityCardHeading)
	if idx < 0 {
		return nil
	}
	return parseIdentityLines(soul[idx:])
}

// parseIdentityLines pulls "- **Field:** value" lines (filled only) out of text.
func parseIdentityLines(text string) []IdentityField {
	var out []IdentityField
	for _, line := range strings.Split(text, "\n") {
		mt := identityFieldRe.FindStringSubmatch(strings.TrimSpace(line))
		if mt == nil {
			continue
		}
		val := strings.TrimSpace(mt[2])
		if val == "" || strings.HasPrefix(val, "_(") { // unfilled placeholder
			continue
		}
		out = append(out, IdentityField{name: mt[1], value: val})
	}
	return out
}

// stripIdentityCard removes the trailing "## Your identity card" block from a
// SOUL.md (the card is always the last section). Used both when carrying a soul
// to a runtime that owns identity elsewhere and when reading a card-bearing soul
// into the bundle (Soul holds only the persona body).
func stripIdentityCard(text string) string {
	idx := strings.Index(text, identityCardHeading)
	if idx < 0 {
		return text
	}
	return strings.TrimRight(text[:idx], " \t\r\n") + "\n"
}

// setIdentityField replaces the first "**field:**" line's value (preserving the
// bullet prefix, dropping a stale italic placeholder hint beneath it), or appends
// "- **field:** value" when no such line exists. Generic line-rewrite used to
// restore identity into an IDENTITY.md without clobbering the rest of the file.
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
// in `_(...)_` or `*(...)*` — a template hint left under an unfilled field, stale
// once the field is filled.
func isItalicPlaceholderLine(line string) bool {
	t := strings.TrimSpace(line)
	if len(t) < 4 {
		return false
	}
	return (strings.HasPrefix(t, "_(") && strings.HasSuffix(t, ")_")) ||
		(strings.HasPrefix(t, "*(") && strings.HasSuffix(t, ")*"))
}

// rebrandEntries applies a brand transform to each entry (writers rebrand the
// canonical, source-branded entries to their own runtime name before merging).
func rebrandEntries(entries []string, brand func(string) string) []string {
	if len(entries) == 0 {
		return nil
	}
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = brand(e)
	}
	return out
}
