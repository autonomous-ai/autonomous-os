package migratepersona

import (
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/lib/usercanon"
)

// Quarantined is one block the memory guard removed from a file, with why.
// The text goes to the `.quarantine.md` sidecar next to the file, never to a
// flow event — it may contain personal notes.
type Quarantined struct {
	Text   string
	Reason string
}

// Reasons a block is quarantined. Stable strings: they are logged, written to
// the sidecar and counted in `memory_changed` flow events.
const (
	ReasonFreeProse    = "free-prose"    // USER.md: filled content outside the `## Users` shape
	ReasonUnknownLabel = "unknown-label" // USER.md: `## Users` entry whose label has no enrollment
	ReasonPrescriptive = "prescriptive"  // names a tool/endpoint and/or says what to DO rather than what happened
)

// WHY A GUARD. USER.md (and Hermes' memories/USER.md) is loaded into the
// system prompt on every turn / session; MEMORY.md likewise. The model treats a
// self-written sentence there as an instruction about HOW TO ACT, and one such
// sentence outranked the whole skill catalogue and the SOUL "Skill priority
// (MANDATORY)" block on lamp-dbda (#421): "…Talks about a personal notebook /
// Obsidian vault notes, wants hands-on action done…" turned "find my keyboard"
// into shell commands and Obsidian lookups. The prompt already forbids this
// (SOUL.md "Memory discipline", the People sync block); a collapsed session
// writes it anyway, and the next hundred sessions inherit it. This is the
// deterministic version of those rules: what the prompt forbids, the OS removes.

// toolRefRe: the block names a tool, app, shell, CLI, endpoint path or file —
// something the agent could ACT with. A person's preferences never need one.
var toolRefRe = regexp.MustCompile(`(?i)(?:` +
	`\b(?:obsidian|notebook|vault|terminal|shell|bash|zsh|exec|curl|wget|ssh|sudo|systemctl|journalctl|python|pip|npm|node|docker|git|cron|cli|api|endpoint|skills?|tools?|commands?|scripts?|prompt|model|llm|servo|camera|mqtt)\b` +
	`|(?:^|[\s(` + "`" + `"'])/[a-z][a-z0-9_-]*(?:/[a-z0-9_{}.-]+)+` + // an endpoint path like /servo/search
	`|\b[a-z0-9_-]+\.(?:md|py|sh|json|ya?ml|js|ts)\b` + // a file name
	`|/dev/` +
	`)`)

// prescriptiveRe: the block says what to DO rather than what HAPPENED. This is
// SOUL.md's second memory-discipline question made mechanical. Deliberately
// broad on directive phrasing; a preference stated as a fact ("prefers
// Vietnamese", "likes jazz") does not match.
//
// The verbs use/run/call/try/avoid/skip only count as prescriptive in
// IMPERATIVE POSITION — at the start of the block, right after
// sentence/segment punctuation, or right after a directive adverb/modal
// (always/never/just/please/should/must/only/then). A bare occurrence
// elsewhere is a troubleshooting OBSERVATION, not an instruction: "Tried to
// use the camera skill but it returned no faces" reports what happened and
// must be kept, unlike "Use the camera skill to find things instead of
// asking".
var prescriptiveRe = regexp.MustCompile(`(?i)(?:\b(?:` +
	`always|never|must|should|do not|don'?t|instead of|rather than` +
	`|prefer(?:s|red)? (?:to|that (?:you|i))` +
	`|match(?:ing)? the|respond(?:ing)? in|repl(?:y|ying) in|answer(?:ing)? in|speak(?:ing)? in` +
	`|hands[- ]on|wants? .{0,40}\bdone|works? best` +
	`|be (?:brief|concise|short|direct)|keep (?:it|replies|answers)` +
	`|when (?:asked|told|the user)` +
	`)\b` +
	`|(?:^|[.;:!]\s+|\b(?:always|never|just|please|should|must|only|then)\s+)(?:use|run|call|try|avoid|skip)\b` +
	`)`)

func namesToolOrEndpoint(s string) bool { return toolRefRe.MatchString(s) }
func prescribesBehaviour(s string) bool { return prescriptiveRe.MatchString(s) }

// isPoisonForMemory is the MEMORY.md rule: a line that names a tool/endpoint
// AND says what to do belongs in a skill or nowhere ("Full-room scan works best
// as curl-driven aim + look per direction" is the shape to refuse). Either half
// alone is fine — "the camera skill returned no faces" is an observation.
func isPoisonForMemory(s string) bool { return namesToolOrEndpoint(s) && prescribesBehaviour(s) }

// ---- block splitting -------------------------------------------------------

type blockKind int

const (
	blockPassthrough blockKind = iota // heading, blank, comment, code, table row, rule
	blockContent                      // a bullet (+ its continuation lines), a paragraph, or a § entry
)

// memBlock is one region of the source file. raw is rejoined verbatim when the
// block is kept, so a clean file round-trips byte for byte — that is what makes
// "write only when something changed" (and so no prompt-cache miss) possible.
type memBlock struct {
	raw  string
	text string // bullet marker / indentation stripped, whitespace normalised (blockContent only)
	kind blockKind
}

var reBulletMarker = regexp.MustCompile(`^\s*(?:[-*]|\d+\.)\s+`)

// splitMemoryBlocks understands both formats the runtimes use: Hermes keeps
// entries separated by "\n§\n" (see entryDelimiter); every other runtime is
// markdown, where a bullet swallows its indented continuation lines and a
// paragraph runs until a blank line, heading, bullet, fence or comment.
func splitMemoryBlocks(raw string) (blocks []memBlock, delimited bool) {
	if strings.Contains(raw, entryDelimiter) {
		for _, part := range strings.Split(raw, entryDelimiter) {
			blocks = append(blocks, memBlock{raw: part, text: normalizeText(part), kind: blockContent})
		}
		return blocks, true
	}
	lines := strings.SplitAfter(raw, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	inCode, inComment := false, false
	for i := 0; i < len(lines); {
		line := lines[i]
		stripped := strings.TrimSpace(line)
		switch {
		case inComment:
			if strings.Contains(stripped, "-->") {
				inComment = false
			}
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case strings.HasPrefix(stripped, "<!--"):
			inComment = !strings.Contains(stripped, "-->")
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case strings.HasPrefix(stripped, "```"):
			inCode = !inCode
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		case inCode, stripped == "", stripped == "---", stripped == "***",
			reHeading.MatchString(stripped),
			strings.HasPrefix(stripped, "|") && strings.HasSuffix(stripped, "|"):
			blocks = append(blocks, memBlock{raw: line, kind: blockPassthrough})
			i++
		default:
			isBullet := reBulletMarker.MatchString(line)
			j := i + 1
			for j < len(lines) {
				next := lines[j]
				ns := strings.TrimSpace(next)
				if ns == "" || reHeading.MatchString(ns) || strings.HasPrefix(ns, "```") || strings.HasPrefix(ns, "<!--") {
					break
				}
				if isBullet && !strings.HasPrefix(next, "  ") && !strings.HasPrefix(next, "\t") {
					break
				}
				if !isBullet && reBulletMarker.MatchString(next) {
					break
				}
				j++
			}
			rawBlock := strings.Join(lines[i:j], "")
			text := rawBlock
			if isBullet {
				text = reBulletMarker.ReplaceAllString(lines[i], "")
				for _, c := range lines[i+1 : j] {
					text += " " + strings.TrimSpace(c)
				}
			}
			blocks = append(blocks, memBlock{raw: rawBlock, text: normalizeText(text), kind: blockContent})
			i = j
		}
	}
	return blocks, false
}

func joinMemoryBlocks(blocks []memBlock, delimited bool) string {
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		parts = append(parts, b.raw)
	}
	if delimited {
		return strings.Join(parts, entryDelimiter)
	}
	return strings.Join(parts, "")
}

// rebuildBlock re-serialises a content block whose text changed, keeping the
// original bullet marker (or none, for a § entry / paragraph).
func rebuildBlock(b memBlock, text string, delimited bool) string {
	if delimited {
		return text
	}
	marker := reBulletMarker.FindString(b.raw)
	return marker + text + "\n"
}

// ---- USER.md ---------------------------------------------------------------

var (
	// A single-underscore / single-star italic hint, e.g. `_(optional)_`,
	// `_Learn about the person you're helping…_`. Bold (`**…**`) is NOT a hint.
	italicHintRe = regexp.MustCompile(`^(?:_[^_]+_|\*[^*]+\*)$`)
	// A bare markdown link, optionally labelled: `Related: [Agent workspace](/concepts/agent-workspace)`.
	linkOnlyRe = regexp.MustCompile(`^(?:[A-Za-z ]+:\s*)?\[[^\]]+\]\([^)]+\)$`)
	// A heading prefix left by an earlier flatten of the file ("Context: ",
	// "Users: ", "A > B: ") — see extractMarkdownEntries. Not content.
	headingPrefixRe = regexp.MustCompile(`^(?:[A-Za-z][A-Za-z ]{0,30}(?: > [A-Za-z][A-Za-z ]{0,30})*):\s+`)
)

// userProfileTemplateSentences are the prose lines of the USER.md template the
// runtimes ship (read off lamp-ac82, see liveDeviceUserMD in user_profile_test.go),
// normalised by normalizeKey. They are the only free prose the file may carry.
var userProfileTemplateSentences = map[string]bool{
	normalizeKey("Learn about the person you're helping. Update this as you go."):                                                                              true,
	normalizeKey("What do they care about? What projects are they working on? What annoys them? What makes them laugh? Build this over time."):                 true,
	normalizeKey("The more you know, the better you can help. But remember — you're learning about a person, not building a dossier. Respect the difference."): true,
	normalizeKey("USER.md - About Your Human"): true,
}

func normalizeKey(s string) string {
	s = strings.ToLower(normalizeText(s))
	s = strings.Trim(s, "_*() ")
	return s
}

// isUserProfileScaffolding reports whether a USER.md content block is part of
// the template rather than something the agent learned: an empty field slot, an
// italic hint, a rule, a link, a known template sentence — or a FILLED singular
// field (Name / What to call them / Pronouns / Timezone), which the enrollment
// retire pass owns and which cannot steer routing.
func isUserProfileScaffolding(text string) bool {
	t := strings.TrimSpace(headingPrefixRe.ReplaceAllString(strings.TrimSpace(text), ""))
	if t == "" || t == "---" || t == "***" {
		return true
	}
	if m := userFieldEntryRe.FindStringSubmatch(t); m != nil {
		if !hasRealFieldValue(m[2]) {
			return true
		}
		return isUserProfileField(strings.TrimSpace(m[1]))
	}
	if italicHintRe.MatchString(t) || linkOnlyRe.MatchString(t) {
		return true
	}
	return userProfileTemplateSentences[normalizeKey(t)]
}

// guardUsersEntry filters the `key: value; …` segments of a `**label (role)**`
// entry. A segment whose value names a tool/endpoint or prescribes behaviour is
// dropped; the rest are kept in order. Returns the input unchanged when nothing
// was dropped so a clean entry is not re-serialised.
func guardUsersEntry(text string) (string, []Quarantined) {
	head := usersBlockRe.FindString(text)
	rest := strings.TrimLeft(strings.TrimSpace(text[len(head):]), "—–-: ")
	if rest == "" {
		return text, nil
	}
	var kept []string
	var dropped []Quarantined
	for _, seg := range strings.Split(rest, ";") {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		value := seg
		// `call: Anh Long` — judge the value, not the key (the key "call" would
		// otherwise trip the "call the/a/it" directive pattern).
		if k, v, ok := strings.Cut(seg, ":"); ok && len(strings.Fields(k)) <= 3 {
			value = v
		}
		if namesToolOrEndpoint(value) || prescribesBehaviour(value) {
			dropped = append(dropped, Quarantined{Text: seg, Reason: ReasonPrescriptive})
			continue
		}
		kept = append(kept, seg)
	}
	if len(dropped) == 0 {
		return text, nil
	}
	if len(kept) == 0 {
		return strings.TrimSpace(head), dropped
	}
	return strings.TrimSpace(head) + " — " + strings.Join(kept, "; "), dropped
}

// GuardUserProfileText applies the strict USER.md allowlist: template
// scaffolding passes, a `**label (role)**` entry passes (label check when
// enrollment is known, segments filtered), everything else is quarantined.
// enrolled is the set of canonical enrollment labels; nil or empty means the
// store could not be read / nobody is enrolled yet, and the label check is
// skipped rather than failing every entry (same stance as the retire pass).
//
// A clean file is returned unchanged with nil dropped — callers must not write
// in that case: USER.md sits in the cached prompt prefix.
func GuardUserProfileText(raw string, enrolled map[string]bool) (string, []Quarantined) {
	blocks, delimited := splitMemoryBlocks(raw)
	var dropped []Quarantined
	out := make([]memBlock, 0, len(blocks))
	for _, b := range blocks {
		if b.kind != blockContent {
			out = append(out, b)
			continue
		}
		if m := usersBlockRe.FindStringSubmatch(b.text); m != nil {
			label := strings.TrimSpace(m[1])
			if len(enrolled) > 0 && !enrolled[usercanon.Resolve(label)] {
				dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonUnknownLabel})
				continue
			}
			kept, segs := guardUsersEntry(b.text)
			if len(segs) > 0 {
				dropped = append(dropped, segs...)
				b.raw = rebuildBlock(b, kept, delimited)
				b.text = kept
			}
			out = append(out, b)
			continue
		}
		if isUserProfileScaffolding(b.text) {
			out = append(out, b)
			continue
		}
		dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonFreeProse})
	}
	if len(dropped) == 0 {
		return raw, nil
	}
	return joinMemoryBlocks(out, delimited), dropped
}

// GuardMemoryText applies the MEMORY.md rule (see isPoisonForMemory) to every
// content block. Free-form observations are untouched. Same "unchanged when
// clean" contract as GuardUserProfileText.
func GuardMemoryText(raw string) (string, []Quarantined) {
	blocks, delimited := splitMemoryBlocks(raw)
	var dropped []Quarantined
	out := make([]memBlock, 0, len(blocks))
	for _, b := range blocks {
		if b.kind == blockContent && isPoisonForMemory(b.text) {
			dropped = append(dropped, Quarantined{Text: b.text, Reason: ReasonPrescriptive})
			continue
		}
		out = append(out, b)
	}
	if len(dropped) == 0 {
		return raw, nil
	}
	return joinMemoryBlocks(out, delimited), dropped
}
