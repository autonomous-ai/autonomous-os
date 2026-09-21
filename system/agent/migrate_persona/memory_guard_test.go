package migratepersona

import (
	"strings"
	"testing"
)

// greenLampPoison is the one line that broke skill routing on lamp-dbda
// (issue #421). It is free prose outside the `## Users` shape.
const greenLampPoison = "- User is multilingual and code-switches freely (Spanish, Vietnamese, Hindi, Portuguese, English) — often within one session. Match the language/tone of each message rather than assuming one fixed language. Talks about a personal notebook / Obsidian vault notes, wants hands-on action done (not theoretical discussion).\n"

const cleanUserMD = `# USER.md - About Your Human

- _Learn about the person you're helping. Update this as you go._
- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**

## Context

_(What do they care about? What projects are they working on? What annoys them? What makes them laugh? Build this over time.)_

---

The more you know, the better you can help. But remember — you're learning about a person, not building a dossier. Respect the difference.

Related: [Agent workspace](/concepts/agent-workspace)

## Users

- **long (friend)** — call: Anh Long; notes: prefers Vietnamese; likes jazz in the evening
`

func TestGuardUserProfileKeepsCleanFileByteForByte(t *testing.T) {
	out, dropped := GuardUserProfileText(cleanUserMD, map[string]bool{"long": true})
	if len(dropped) != 0 {
		t.Fatalf("clean file must drop nothing, got %+v", dropped)
	}
	if out != cleanUserMD {
		t.Fatalf("clean file must round-trip byte for byte:\n%s", out)
	}
}

func TestGuardUserProfileQuarantinesTheGreenLampLine(t *testing.T) {
	raw := cleanUserMD + greenLampPoison
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
	if len(dropped) != 1 || dropped[0].Reason != ReasonFreeProse {
		t.Fatalf("want exactly the poison line dropped as free prose, got %+v", dropped)
	}
	if !strings.Contains(dropped[0].Text, "Obsidian") {
		t.Errorf("dropped text should be the poison line, got %q", dropped[0].Text)
	}
	if out != cleanUserMD {
		t.Errorf("everything else must survive untouched:\n%s", out)
	}
}

func TestGuardUserProfileQuarantinesFilledNotesAndParagraphProse(t *testing.T) {
	raw := "- **Name:**\n- **Notes:** wants everything done via the terminal\n\nAlways answer in Spanish.\n\n## Users\n\n- **long (friend)** — notes: prefers tea\n"
	out, dropped := GuardUserProfileText(raw, nil)
	if len(dropped) != 2 {
		t.Fatalf("want the Notes bullet and the paragraph dropped, got %+v", dropped)
	}
	// Removed blocks take their own lines only; the blank lines around them
	// stay (two blanks remain between Name and ## Users). The guard does not
	// reflow the file.
	want := "- **Name:**\n\n\n## Users\n\n- **long (friend)** — notes: prefers tea\n"
	if out != want {
		t.Errorf("want:\n%s\ngot:\n%s", want, out)
	}
}

func TestGuardUserProfileKeepsFilledSingularFields(t *testing.T) {
	// A filled Name/Timezone is the retire pass's business, not the guard's.
	raw := "- **Name:** Long\n- **Timezone:** Asia/Ho_Chi_Minh\n"
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
	if len(dropped) != 0 || out != raw {
		t.Fatalf("singular fields must pass through, got %+v\n%s", dropped, out)
	}
}

func TestGuardUsersEntryStripsPrescriptiveSegments(t *testing.T) {
	raw := "## Users\n\n- **long (friend)** — call: Anh Long; notes: prefers Vietnamese; wants hands-on action done, not discussion; keeps notes in an Obsidian vault; match the language of each message; likes jazz\n"
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
	if len(dropped) != 3 {
		t.Fatalf("want 3 segments dropped, got %d: %+v", len(dropped), dropped)
	}
	for _, d := range dropped {
		if d.Reason != ReasonPrescriptive {
			t.Errorf("segment %q should be %s, got %s", d.Text, ReasonPrescriptive, d.Reason)
		}
	}
	want := "## Users\n\n- **long (friend)** — call: Anh Long; notes: prefers Vietnamese; likes jazz\n"
	if out != want {
		t.Errorf("want:\n%s\ngot:\n%s", want, out)
	}
}

// TestGuardUsersEntryKeyValueSegmentsHitTheImperativeRule covers F2: the value
// of a `key: value` segment used to keep its leading space, so the
// imperative-position branch (`^use|run|…`) never saw the verb at the start
// and `notes: run a full scan…` was kept.
func TestGuardUsersEntryKeyValueSegmentsHitTheImperativeRule(t *testing.T) {
	raw := "- **long (friend)** — call: Anh Long; notes: run a full scan of the room first; notes: skip greetings; notes: use gestures; likes jazz\n"
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
	if len(dropped) != 3 {
		t.Fatalf("want the three imperative values dropped, got %d: %+v", len(dropped), dropped)
	}
	want := "- **long (friend)** — call: Anh Long; likes jazz\n"
	if out != want {
		t.Errorf("want:\n%s\ngot:\n%s", want, out)
	}
}

// TestGuardUsersEntryKeepsOrdinaryFacts covers F3: `## Users` segments are
// what the People-sync heartbeat re-adds every ~30 min, so a false positive
// there is a write loop (new .bak, sidecar entry, red badge — forever). The
// segment rule therefore has no bare always/never/must/should/when-asked and
// no generic tool nouns (python, git, camera, …); those stay in the MEMORY.md
// rule, where they must co-occur with a tool reference to trip.
func TestGuardUsersEntryKeepsOrdinaryFacts(t *testing.T) {
	keep := []string{
		"always at the desk by 9",
		"never drinks coffee",
		"should graduate in June",
		"when asked about work gets stressed",
		"prefers to be called by first name",
		"learning python at school",
		"likes photography with an old camera",
		"has a dog named Git",
	}
	drop := []string{
		"never use the camera",
		"always run the terminal first",
		"skip greetings",
		"run a full scan of the room first",
		"match the language of each message",
		"wants hands-on action done",
		"keeps notes in an Obsidian vault",
	}
	for _, seg := range keep {
		raw := "- **long (friend)** — " + seg + "\n"
		out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
		if len(dropped) != 0 || out != raw {
			t.Errorf("ordinary fact %q must be KEPT, got dropped=%+v out=%q", seg, dropped, out)
		}
	}
	for _, seg := range drop {
		raw := "- **long (friend)** — " + seg + "\n"
		out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
		if len(dropped) != 1 || out != "- **long (friend)**\n" {
			t.Errorf("directive %q must be DROPPED, got dropped=%+v out=%q", seg, dropped, out)
		}
	}
}

func TestGuardUsersEntryWithEverythingStrippedKeepsTheLabel(t *testing.T) {
	raw := "- **long (friend)** — always run the terminal first\n"
	out, _ := GuardUserProfileText(raw, nil)
	if out != "- **long (friend)**\n" {
		t.Errorf("label must survive when all segments go, got %q", out)
	}
}

func TestGuardUserProfileDropsEntryForUnenrolledLabel(t *testing.T) {
	raw := "## Users\n\n- **stranger_4 (friend)** — notes: sat at the desk\n- **long (friend)** — notes: prefers tea\n"
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true})
	if len(dropped) != 1 || dropped[0].Reason != ReasonUnknownLabel {
		t.Fatalf("want stranger_4 dropped as unknown-label, got %+v", dropped)
	}
	if strings.Contains(out, "stranger_4") || !strings.Contains(out, "**long (friend)**") {
		t.Errorf("wrong entry removed:\n%s", out)
	}
	// With no enrollment knowledge the label check is skipped, not failed.
	if _, dropped := GuardUserProfileText(raw, nil); len(dropped) != 0 {
		t.Errorf("nil enrollment must skip the label check, got %+v", dropped)
	}
}

func TestGuardUserProfileHermesDelimitedFormat(t *testing.T) {
	raw := "**long (friend)** — notes: prefers tea\n§\nUser wants hands-on action, use the shell for everything\n§\n**chloe (friend)** — call: Chloe\n"
	out, dropped := GuardUserProfileText(raw, map[string]bool{"long": true, "chloe": true})
	if len(dropped) != 1 || dropped[0].Reason != ReasonFreeProse {
		t.Fatalf("want the prose entry dropped, got %+v", dropped)
	}
	want := "**long (friend)** — notes: prefers tea\n§\n**chloe (friend)** — call: Chloe\n"
	if out != want {
		t.Errorf("want:\n%s\ngot:\n%s", want, out)
	}
}

func TestGuardMemoryTextQuarantinesEndpointPrescriptions(t *testing.T) {
	raw := "# Memory\n\n<!-- scaffold\nstill scaffold -->\n\n- 2026-09-10: Long asked for jazz in the evening twice this week\n- Full-room scan works best as curl-driven aim + look per direction\n- The camera skill returned no faces on 2026-09-11\n\nWhen asked to find things, always use /servo/aim then /camera/snapshot instead of the search skill.\n"
	out, dropped := GuardMemoryText(raw)
	if len(dropped) != 2 {
		t.Fatalf("want the curl line and the /servo paragraph dropped, got %+v", dropped)
	}
	want := "# Memory\n\n<!-- scaffold\nstill scaffold -->\n\n- 2026-09-10: Long asked for jazz in the evening twice this week\n- The camera skill returned no faces on 2026-09-11\n\n"
	if out != want {
		t.Errorf("want:\n%s\ngot:\n%s", want, out)
	}
}

func TestGuardMemoryTextKeepsCleanFileByteForByte(t *testing.T) {
	raw := "- Long is usually at the desk from 09:00\n- Music: jazz in the evening\n"
	out, dropped := GuardMemoryText(raw)
	if len(dropped) != 0 || out != raw {
		t.Fatalf("clean MEMORY.md must round-trip, got %+v\n%s", dropped, out)
	}
}

// TestGuardMemoryTextKeepsTroubleshootingObservations covers the bare-verb
// trap: "use" (and its siblings run/call/try/avoid/skip) is only prescriptive
// in imperative position. A troubleshooting report of what happened ("Tried
// to use X but...") must survive; an imperative ("Use X to...") must not.
func TestGuardMemoryTextKeepsTroubleshootingObservations(t *testing.T) {
	observation := "- Tried to use the camera skill but it returned no faces\n"
	imperative := "- Use the camera skill to find things instead of asking\n"

	if out, dropped := GuardMemoryText(observation); len(dropped) != 0 || out != observation {
		t.Fatalf("troubleshooting observation must be kept, got %+v\n%s", dropped, out)
	}
	if out, dropped := GuardMemoryText(imperative); len(dropped) != 1 || dropped[0].Reason != ReasonPrescriptive {
		t.Fatalf("imperative use must be dropped as prescriptive, got %+v\n%s", dropped, out)
	}

	raw := observation + imperative
	out, dropped := GuardMemoryText(raw)
	if len(dropped) != 1 || dropped[0].Reason != ReasonPrescriptive {
		t.Fatalf("want exactly the imperative line dropped, got %+v", dropped)
	}
	if out != observation {
		t.Errorf("want:\n%s\ngot:\n%s", observation, out)
	}
}
