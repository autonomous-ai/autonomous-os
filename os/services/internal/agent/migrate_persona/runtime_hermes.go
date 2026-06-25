package migratepersona

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// hermesAdapter reads/writes the Hermes home layout (~/.hermes/). Hermes reads
// SOUL.md as its identity and has NO separate IDENTITY.md slot, so it inlines the
// owner's identity as a card in SOUL; and it loads only MEMORY.md + USER.md by
// name (no KNOWLEDGE/daily slots), so its write FOLDS Knowledge + Daily into the
// single MEMORY.md. That fold is the one structural asymmetry of a round-trip
// through Hermes (content is preserved, structure is flattened).
type hermesAdapter struct{}

func (hermesAdapter) runtime() Runtime { return RuntimeHermes }

func (hermesAdapter) read(opts Options) (*PersonaBundle, error) {
	root := opts.HermesRoot
	mem := filepath.Join(root, "memories")
	rawSoul, _ := os.ReadFile(filepath.Join(root, "SOUL.md"))
	soul := string(rawSoul)

	return &PersonaBundle{
		Soul:     stripIdentityCard(soul), // persona body; card → Identity
		Identity: identityCardFields(soul),
		Memory:   parseEntries(filepath.Join(mem, "MEMORY.md")),
		User:     parseEntries(filepath.Join(mem, "USER.md")),
		// Knowledge / Daily: Hermes has no such slots → nil.
	}, nil
}

func (hermesAdapter) write(m *baseMigrator, b *PersonaBundle, opts Options) error {
	root := opts.HermesRoot
	mem := filepath.Join(root, "memories")
	soulDest := filepath.Join(root, "SOUL.md")

	// Persona → SOUL.md (rebranded). Identity is NOT in the body — it is inlined
	// separately below so the card lands even if SOUL was pre-written.
	m.writePersona("soul", rebrandToHermes(b.Soul), soulDest)

	// Identity → inlined card in SOUL (Hermes has no IDENTITY.md slot). Idempotent
	// append, independent of the writePersona above.
	m.inlineIdentityCard(soulDest, buildIdentityBlockFromFields(b.Identity))

	// Long-term memory → MEMORY.md, FOLDING Knowledge + Daily in (no separate
	// slots). Memory first, then Knowledge (distilled, high-signal — wins dedup +
	// char budget), then daily logs.
	all := append(append(append([]string{}, b.Memory...), b.Knowledge...), b.Daily...)
	m.writeMemoryEntries("memory", rebrandEntries(all, rebrandToHermes),
		filepath.Join(mem, "MEMORY.md"), opts.MemoryCharLimit, hermesFormat)

	// User profile → memories/USER.md.
	m.writeMemoryEntries("user-profile", rebrandEntries(b.User, rebrandToHermes),
		filepath.Join(mem, "USER.md"), opts.UserCharLimit, hermesFormat)
	return nil
}

// buildIdentityBlockFromFields renders the owner's identity fields as a
// "## Your identity card" block for inlining into a Hermes SOUL.md. Returns ""
// when there are no fields (inlineIdentityCard then no-ops). The body is rebranded
// to Hermes for brand consistency with the surrounding soul.
func buildIdentityBlockFromFields(fields []IdentityField) string {
	if len(fields) == 0 {
		return ""
	}
	lines := make([]string, len(fields))
	for i, f := range fields {
		lines[i] = "- **" + f.name + ":** " + f.value
	}
	body := rebrandToHermes(strings.Join(lines, "\n"))
	return "\n\n" + identityCardHeading + "\n\n" +
		"Your owner set this — it overrides any default name or vibe above.\n\n" +
		body + "\n"
}

// buildIdentityBlock reads an IDENTITY.md and renders its filled fields as a
// Hermes identity card. Thin wrapper over readIdentityFields +
// buildIdentityBlockFromFields; retained as the unit-tested entry point.
func buildIdentityBlock(identityPath string) string {
	return buildIdentityBlockFromFields(readIdentityFields(identityPath))
}

// Brand rewriting to Hermes — case-preserving. Rebrands the names of OTHER
// runtimes (OpenClaw and its aliases) onto Hermes when a persona/memory arrives.
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
	text = rePicoClaw.ReplaceAllStringFunc(text, repl)
	return text
}
