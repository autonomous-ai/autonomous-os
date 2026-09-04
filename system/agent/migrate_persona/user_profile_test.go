package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// liveDeviceUserMD is the USER.md read off lamp-ac82 on 2026-09-03, byte-identical
// in .openclaw / .codex / .opencode / .picoclaw. It is the regression fixture:
// two **Name:** bullets (one empty template slot, one filled with a user who
// stopped using the device in June) plus the doc template merged in as data.
const liveDeviceUserMD = `- _Learn about the person you're helping. Update this as you go._
- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**
- **Notes:**
- Context: _(What do they care about? What projects are they working on? What annoys them? What makes them laugh? Build this over time.)_
- Context: ---
- Context: The more you know, the better you can help. But remember — you're learning about a person, not building a dossier. Respect the difference.
- Related: [Agent workspace](/concepts/agent-workspace)
- **Name:** Leo
- Context: Leo speaks Vietnamese primarily (vi). He tends to respond well to music suggestions when in a low mood.
`

func writeTempUserMD(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "USER.md")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatalf("seed USER.md: %v", err)
	}
	return p
}

func runUserProfileWrite(t *testing.T, dest string, incoming []string) string {
	t.Helper()
	m := &baseMigrator{opts: Options{Execute: true}.withDefaults()}
	m.writeUserProfile("user-profile", incoming, dest, DefaultUserCharLimit, openclawFormat)
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read result: %v", err)
	}
	return string(got)
}

// The bug: a new name was APPENDED next to the old one, so the profile could
// gain a name but never retire one.
func TestWriteUserProfileReplacesNameInsteadOfAppending(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("new name not written:\n%s", got)
	}
	if n := strings.Count(got, "**Name:**"); n != 1 {
		t.Errorf("want exactly one Name bullet, got %d:\n%s", n, got)
	}
	if strings.Contains(got, "**Name:** Leo") {
		t.Errorf("retired name survived as a field:\n%s", got)
	}

	// SCOPE: only the singular FIELD is retired. Free-form prose that happens to
	// mention the old user ("Leo speaks Vietnamese…") is ordinary learned content
	// and stays — entry-merge is additive by design. Removing stale prose is the
	// device cleanup's job (plan step C) and, ongoing, the enrollment-keyed prune
	// (B2a). Asserted here so the boundary is not mistaken for a leak.
	if !strings.Contains(got, "Leo speaks Vietnamese") {
		t.Errorf("free-form entries must be left alone by field replacement:\n%s", got)
	}
}

// USER.md is a FORM. The whole template — instructions, prompts, unfilled
// slots, separator, docs link — must survive a migration verbatim; only the
// filled fields change. Anything less and the agent loses guidance it reads on
// every turn, including the only line telling it to maintain this file.
func TestWriteUserProfileKeepsTheEntireTemplate(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	for _, want := range []string{
		"- _Learn about the person you're helping. Update this as you go._",
		"- **What to call them:**",
		"- **Pronouns:** _(optional)_",
		"- **Timezone:**",
		"- **Notes:**",
		"- Context: _(What do they care about?",
		"- Context: ---",
		"- Context: The more you know, the better you can help.",
		"- Related: [Agent workspace](/concepts/agent-workspace)",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("template line %q was lost:\n%s", want, got)
		}
	}
}

// The blank slot is FILLED where it stands — the template keeps its shape and
// ordering, and the retired value that earlier merges appended below it goes.
func TestWriteUserProfileFillsTheSlotInPlace(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	lines := strings.Split(strings.TrimRight(got, "\n"), "\n")
	if len(lines) < 2 || lines[1] != "- **Name:** Long" {
		t.Errorf("Name must be filled in the slot it already occupies (line 2), got:\n%s", got)
	}
	if !strings.HasPrefix(lines[0], "- _Learn about the person") {
		t.Errorf("the instruction must stay above the fields:\n%s", got)
	}
}

// Instruction is carried, not accumulated: dedupe keeps exactly one copy however
// many migrations run.
func TestWriteUserProfileDoesNotAccumulateInstructions(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	runUserProfileWrite(t, dest, splitLines(liveDeviceUserMD))
	got := runUserProfileWrite(t, dest, splitLines(liveDeviceUserMD))
	if n := strings.Count(got, "Update this as you go"); n != 1 {
		t.Errorf("want one copy of the instruction, got %d:\n%s", n, got)
	}
}

// splitLines turns a rendered USER.md back into the entries a source adapter
// would hand the writer.
func splitLines(doc string) []string {
	var out []string
	for _, l := range strings.Split(doc, "\n") {
		if e := strings.TrimPrefix(strings.TrimSpace(l), "- "); e != "" {
			out = append(out, e)
		}
	}
	return out
}

// A source runtime with no profile yet must never erase the name the device
// already learned. This is the guard against "retire on absence".
func TestWriteUserProfileNeverBlanksAFilledField(t *testing.T) {
	seeded := "- **Name:** Long\n- Context: Prefers Vietnamese.\n"

	for name, incoming := range map[string][]string{
		"no incoming entries at all": nil,
		"incoming has empty Name":    {"**Name:**"},
		"incoming Name is a hint":    {"**Name:** _(who are they?)_"},
		"incoming is unrelated":      {"Context: Likes lo-fi."},
	} {
		t.Run(name, func(t *testing.T) {
			dest := writeTempUserMD(t, seeded)
			got := runUserProfileWrite(t, dest, incoming)
			if !strings.Contains(got, "- **Name:** Long") {
				t.Errorf("existing name was lost:\n%s", got)
			}
		})
	}
}

// Free-form content stays additive — only the singular fields are replaced.
func TestWriteUserProfileStillMergesFreeFormEntries(t *testing.T) {
	dest := writeTempUserMD(t, "- **Name:** Long\n- Context: Prefers Vietnamese.\n")
	got := runUserProfileWrite(t, dest, []string{"Context: Works late on Fridays."})

	for _, want := range []string{"Prefers Vietnamese.", "Works late on Fridays."} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q — free-form entries must merge, not replace:\n%s", want, got)
		}
	}
}

// A destination whose template has no slot for the field (a runtime with a
// different USER.md shape) gets the bullet appended — the "or-append" half,
// same as setIdentityField does for IDENTITY.md.
func TestWriteUserProfileAppendsWhenThereIsNoSlot(t *testing.T) {
	dest := writeTempUserMD(t, "- Context: An earlier note.\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("field not written when no slot exists:\n%s", got)
	}
	if !strings.Contains(got, "- Context: An earlier note.") {
		t.Errorf("existing content lost:\n%s", got)
	}
}

// An unfilled slot for a field we have no value for stays as it is — the form
// is still asking, and the agent reads that prompt every turn.
func TestWriteUserProfileLeavesUnknownFieldSlotsBlank(t *testing.T) {
	dest := writeTempUserMD(t, "- **Name:**\n- **Timezone:**\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Timezone:**\n") {
		t.Errorf("blank slot for an unknown field must be preserved:\n%s", got)
	}
	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("known field not filled:\n%s", got)
	}
}

// A bold bullet that is not one of the singular fields must not be silently
// dropped just because it looks like a field.
func TestWriteUserProfileKeepsNonProfileBoldBullets(t *testing.T) {
	dest := writeTempUserMD(t, "- **Notes:** Allergic to cilantro.\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	if !strings.Contains(got, "Allergic to cilantro") {
		t.Errorf("non-profile bold bullet was dropped:\n%s", got)
	}
}

// Migration must converge: running it twice changes nothing the second time.
func TestWriteUserProfileIsIdempotent(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	first := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	second := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	if first != second {
		t.Errorf("not idempotent:\nfirst:\n%s\nsecond:\n%s", first, second)
	}
}

func TestWriteUserProfileDryRunTouchesNothing(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	m := &baseMigrator{opts: Options{Execute: false}.withDefaults()}
	m.writeUserProfile("user-profile", []string{"**Name:** Long"}, dest, DefaultUserCharLimit, openclawFormat)

	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(got) != liveDeviceUserMD {
		t.Errorf("dry run modified the file:\n%s", got)
	}
}

func TestPartitionUserFieldsPrefersTheLaterFilledDuplicate(t *testing.T) {
	fields, _ := partitionUserFields([]string{"**Name:** Leo", "**Name:** Long"})
	if len(fields) != 1 || fields[0].value != "Long" {
		t.Fatalf("want the later value to win, got %+v", fields)
	}
}
