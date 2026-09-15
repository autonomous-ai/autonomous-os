package hermes

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// legacySkillPriorityBlock is how the block looked on devices that ran an
// os-server before it got its own delimiter: same body, wrapped in soulOSMarker.
func legacySkillPriorityBlock() string {
	return soulOSMarker + "\n" + soulSkillPrioritySentinel + " old wording\n---\n"
}

func TestUpsertSoulSkillPriorityBlock_AppendsBelowPersona(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n"
	got := upsertSoulSkillPriorityBlock(in)
	if !strings.HasPrefix(got, "# Soul\n\nYou are Lamp.\n") {
		t.Fatalf("persona content not preserved at top:\n%q", got)
	}
	if !strings.Contains(got, soulSkillPriorityMarker) {
		t.Fatalf("marker missing:\n%q", got)
	}
	if !strings.Contains(got, "`connectors` skill") {
		t.Fatalf("connectors rule missing:\n%q", got)
	}
	if !strings.HasSuffix(got, "---\n") {
		t.Fatalf("block must close with --- separator:\n%q", got)
	}
}

func TestUpsertSoulSkillPriorityBlock_Idempotent(t *testing.T) {
	once := upsertSoulSkillPriorityBlock("# Soul\n\npersona\n")
	twice := upsertSoulSkillPriorityBlock(once)
	if once != twice {
		t.Fatalf("not idempotent:\n once=%q\ntwice=%q", once, twice)
	}
}

func TestUpsertSoulSkillPriorityBlock_ReplacesStaleBlock(t *testing.T) {
	stale := "# Soul\n\npersona\n\n" + soulSkillPriorityMarker + "\n" +
		soulSkillPrioritySentinel + " OLD RULE\n---\n"
	got := upsertSoulSkillPriorityBlock(stale)
	if strings.Contains(got, "OLD RULE") {
		t.Fatalf("stale block content survived:\n%q", got)
	}
	if n := strings.Count(got, soulSkillPriorityMarker); n != 1 {
		t.Fatalf("want exactly one marker block, got %d:\n%q", n, got)
	}
	if !strings.Contains(got, "persona") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

// A device updating from an older os-server carries the block under the shared
// marker. It must be replaced in place, not left behind as a second copy.
func TestUpsertSoulSkillPriorityBlock_ReplacesLegacyMarkerBlock(t *testing.T) {
	got := upsertSoulSkillPriorityBlock("# Soul\n\npersona\n\n" + legacySkillPriorityBlock())
	if strings.Contains(got, "old wording") {
		t.Fatalf("legacy block survived:\n%q", got)
	}
	if strings.Contains(got, soulOSMarker) {
		t.Fatalf("legacy marker left behind:\n%q", got)
	}
	if n := strings.Count(got, soulSkillPrioritySentinel); n != 1 {
		t.Fatalf("want exactly one skill-priority body, got %d:\n%q", n, got)
	}
}

// The regression this whole split exists for: a migrated SOUL.md opens with the
// persona wrapped in soulOSMarker (openclaw's ensureSoulMDBlock shape). Sharing
// one marker made the upsert strip it on the next boot.
func TestUpsertSoulSkillPriorityBlock_KeepsMarkedPersonaBlock(t *testing.T) {
	persona := soulOSMarker + "\n# Lamp\n\n## Skill-driven turns (Non-Negotiable)\n" +
		"- `[sensing:*]` → `skills/sensing/SKILL.md`.\n---\n"
	got := upsertSoulSkillPriorityBlock(persona + "\nowner notes\n")

	if !strings.Contains(got, "Skill-driven turns") {
		t.Fatalf("persona deleted:\n%q", got)
	}
	if !strings.Contains(got, "`[sensing:*]`") {
		t.Fatalf("sensing routing rule deleted:\n%q", got)
	}
	if !strings.Contains(got, "owner notes") {
		t.Fatalf("owner content deleted:\n%q", got)
	}
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("persona block must stay at the top:\n%q", got)
	}
	if upsertSoulSkillPriorityBlock(got) != got {
		t.Fatalf("not idempotent with a persona block present")
	}
}

func TestUpsertSoulSkillPriorityBlock_EmptySoul(t *testing.T) {
	got := upsertSoulSkillPriorityBlock("")
	if !strings.HasPrefix(got, soulSkillPriorityMarker) {
		t.Fatalf("empty soul should become just the block:\n%q", got)
	}
	if upsertSoulSkillPriorityBlock(got) != got {
		t.Fatalf("not idempotent on block-only soul")
	}
}

func TestUpsertSoulSkillPriorityBlock_PreservesIdentityCard(t *testing.T) {
	// The identity card (identity.go / persona migration) must survive the
	// strip+re-append — only the marker-delimited block is managed.
	in := "# Soul\n\n## Your identity card\n\n- **Name:** Ngân\n\n" +
		soulSkillPriorityMarker + "\n" + soulSkillPrioritySentinel + " old\n---\n"
	got := upsertSoulSkillPriorityBlock(in)
	if !strings.Contains(got, "- **Name:** Ngân") {
		t.Fatalf("identity card lost:\n%q", got)
	}
	if idx := strings.Index(got, soulSkillPriorityMarker); idx < strings.Index(got, "identity card") {
		t.Fatalf("block should sit below the identity card:\n%q", got)
	}
}

const testPersona = "# Lamp\n\n## Skill-driven turns (Non-Negotiable)\n- `[sensing:*]` → `skills/sensing/SKILL.md`."

func TestUpsertSoulPersonaBlock_SeedsEmptySoul(t *testing.T) {
	got := upsertSoulPersonaBlock("", testPersona)
	if !strings.HasPrefix(got, soulOSMarker+"\n# Lamp") {
		t.Fatalf("persona block missing from a fresh soul:\n%q", got)
	}
	if !strings.Contains(got, "\n---\n") {
		t.Fatalf("block must close with --- separator:\n%q", got)
	}
	// Same owner-editable section every other runtime seeds on a first install.
	if !strings.Contains(got, soulPersonalHeading) {
		t.Fatalf("owner-editable section missing:\n%q", got)
	}
	if upsertSoulPersonaBlock(got, testPersona) != got {
		t.Fatal("not idempotent on a freshly seeded soul")
	}
}

// A managed default soul below the block is a second, competing persona — the
// duplication bug openclaw/picoclaw guard against. Hermes gets its own default
// from hermesSoulFallback on a factory reset before a soul_ref is declared.
func TestUpsertSoulPersonaBlock_DropsManagedDefaultSoul(t *testing.T) {
	for name, stale := range map[string]string{
		"hermes fallback":  hermesSoulFallback,
		"openclaw seed":    "# Soul\n\nold template\n",
		"openclaw gateway": "# SOUL.md - Who You Are\n\nold template\n",
		// What presync leaves behind on a freshly flashed device: the Hermes
		// gateway re-seeds its own persona whenever SOUL.md is missing, and it
		// opens with prose rather than a heading.
		"hermes gateway seed": "You are Hermes Agent, built by Nous Research. Be direct.\n\nold template\n",
	} {
		got := upsertSoulPersonaBlock(stale, testPersona)
		if strings.Contains(got, "old template") || strings.Contains(got, "Hermes Agent Persona") ||
			strings.Contains(got, "Nous Research") {
			t.Errorf("%s: managed default kept as owner content:\n%q", name, got)
		}
		if !strings.Contains(got, soulPersonalHeading) {
			t.Errorf("%s: owner-editable section missing:\n%q", name, got)
		}
	}
}

// Owner edits under `## Personal` survive even when a managed default sits above
// them — only the default itself is discarded.
func TestUpsertSoulPersonaBlock_KeepsPersonalSectionUnderDefault(t *testing.T) {
	stale := "# Soul\n\nold template\n\n" + soulPersonalHeading + "\n\nI drink tea at 3pm.\n"
	got := upsertSoulPersonaBlock(stale, testPersona)

	if strings.Contains(got, "old template") {
		t.Fatalf("managed default survived:\n%q", got)
	}
	if !strings.Contains(got, "I drink tea at 3pm.") {
		t.Fatalf("owner edits lost:\n%q", got)
	}
}

func TestUpsertSoulPersonaBlock_KeepsOwnerContent(t *testing.T) {
	got := upsertSoulPersonaBlock("## Personal\n\nowner notes\n", testPersona)
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("persona must sit at the top:\n%q", got)
	}
	if !strings.Contains(got, "owner notes") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

// An OTA ships new persona wording: the old block is replaced, not stacked.
func TestUpsertSoulPersonaBlock_RefreshesWithoutDuplicating(t *testing.T) {
	first := upsertSoulPersonaBlock("", testPersona)
	got := upsertSoulPersonaBlock(first, "# Lamp v2\n\nnew wording")

	if strings.Contains(got, "Skill-driven turns") {
		t.Fatalf("stale persona survived:\n%q", got)
	}
	if n := strings.Count(got, soulOSMarker); n != 1 {
		t.Fatalf("want exactly one persona block, got %d:\n%q", n, got)
	}
	if !strings.Contains(got, "new wording") {
		t.Fatalf("new persona missing:\n%q", got)
	}
}

// The skill-priority block is not a persona — the persona upsert must leave it
// alone, whether it wears the current marker or the legacy shared one.
func TestUpsertSoulPersonaBlock_LeavesSkillPriorityBlockAlone(t *testing.T) {
	for name, soul := range map[string]string{
		"current": upsertSoulSkillPriorityBlock(""),
		"legacy":  legacySkillPriorityBlock(),
	} {
		got := upsertSoulPersonaBlock(soul, testPersona)
		if !strings.Contains(got, soulSkillPrioritySentinel) {
			t.Fatalf("%s: skill-priority block deleted:\n%q", name, got)
		}
	}
}

// Both blocks run back to back on every boot, in the order EnsureOnboarding
// calls them. The file must settle after the first pass and never grow again.
func TestSoulBlocks_StableAcrossBoots(t *testing.T) {
	boot := func(soul string) string {
		return upsertSoulSkillPriorityBlock(upsertSoulPersonaBlock(soul, testPersona))
	}

	first := boot("")
	second := boot(first)
	if first != second {
		t.Fatalf("second boot rewrote SOUL.md:\n first=%q\nsecond=%q", first, second)
	}
	if n := strings.Count(second, soulOSMarker); n != 1 {
		t.Fatalf("want one persona block, got %d", n)
	}
	if n := strings.Count(second, soulSkillPriorityMarker); n != 1 {
		t.Fatalf("want one skill-priority block, got %d", n)
	}
	if !strings.Contains(second, "`[sensing:*]`") {
		t.Fatalf("sensing routing rule missing after two boots:\n%q", second)
	}
	if strings.Index(second, soulOSMarker) > strings.Index(second, soulSkillPriorityMarker) {
		t.Fatalf("persona must precede the skill-priority block:\n%q", second)
	}
}

// A device migrated from OpenClaw arrives with the persona in a marked block and
// owner content plus the inlined identity card below it (see
// system/agent/migrate_persona). Two boots later all of it must still be there,
// exactly once.
func TestSoulBlocks_MigratedDeviceKeepsPersonaAndOwnerContent(t *testing.T) {
	migrated := soulOSMarker + "\n" + testPersona + "\n---\n\n## Personal\n\nowner notes\n" +
		"\n## Your identity card\n\n- **Name:** Ngân\n"

	got := upsertSoulSkillPriorityBlock(upsertSoulPersonaBlock(migrated, testPersona))
	got = upsertSoulSkillPriorityBlock(upsertSoulPersonaBlock(got, testPersona))

	for _, want := range []string{"Skill-driven turns", "owner notes", "- **Name:** Ngân", soulSkillPrioritySentinel} {
		if !strings.Contains(got, want) {
			t.Fatalf("%q lost:\n%q", want, got)
		}
	}
	if n := strings.Count(got, "owner notes"); n != 1 {
		t.Fatalf("owner content duplicated %d times", n)
	}
}

func mkSkill(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte("# "+name), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPruneImportedDuplicates_RemovesWhenBaseExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	mkSkill(t, dir, "connectors-imported")
	mkSkill(t, dir, "voice")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors-imported")); !os.IsNotExist(err) {
		t.Fatalf("duplicate not removed")
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors", "SKILL.md")); err != nil {
		t.Fatalf("canonical copy lost: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "voice")); err != nil {
		t.Fatalf("unrelated skill touched: %v", err)
	}
}

func TestPruneImportedDuplicates_RenamesWhenOnlyImportedExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "standup-imported")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "standup", "SKILL.md")); err != nil {
		t.Fatalf("imported-only skill not renamed to canonical name: %v", err)
	}
}

func TestPruneImportedDuplicates_NoopCases(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	// plain file with the suffix must be ignored (not a skill dir)
	if err := os.WriteFile(filepath.Join(dir, "notes-imported"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := pruneImportedDuplicatesIn(dir); got != 0 {
		t.Fatalf("changed = %d, want 0", got)
	}
	if got := pruneImportedDuplicatesIn(filepath.Join(dir, "absent")); got != 0 {
		t.Fatalf("absent dir: changed = %d, want 0", got)
	}
}
