package http

// Go port of HAL os/hal/drivers/voice/_internal/cot_leak_filter.py — filter
// chain-of-thought leaks out of agent reply text before it reaches TTS, the
// web chat (full_text), and channel fan-out (Telegram DM/broadcast, Slack).
//
// DeepSeek-style models running behind openclaw/hermes sometimes emit their
// whole English planning monologue as plain assistant text ahead of the real
// reply ("The `[emotion_context]` shows `mapped_mood: "sad"` ... Route =
// **music**. I need to log the signal ... [nhẹ nhàng] Có vẻ hơi trầm ...").
// The HAL filter only guards the realtime (Gemini Live) transcript path; this
// port guards the main-agent path in os-server.
//
// Same three tiers as the Python filter (keep the two in sync when hardening
// either side):
//   - TRIGGER markers — meta-discourse that can never occur in a reply spoken
//     TO the user. Always dropped; they switch the turn into CoT mode.
//   - SECONDARY markers — meta terms a reply could legitimately contain.
//     Dropped only once CoT mode is already on.
//   - CoT-mode continuation — English-looking sentences (only when the reply
//     language is not English), quoted draft fragments, bare plan runts, and
//     fuzzy near-duplicates of already-kept sentences.
//
// Go-side addition over the Python original: snake_case identifiers
// (emotion_context, user_info, telegram_id, ...) join the TRIGGER tier — the
// DeepSeek leak corpus opens with context-field analysis instead of the
// "The user is/wants..." phrasing the Gemini corpus starts with, and a
// snake_case token never occurs in a sentence meant to be spoken aloud.
//
// Language handling mirrors the Python filter, except the constructor takes
// the config.json stt_language BCP-47 code ("vi", "zh-CN", ...) directly
// instead of a human-readable name. "" (unset) is treated as English → only
// the marker tiers apply, so a legitimate English reply is never at risk from
// the heuristics.

import (
	"regexp"
	"strings"
	"unicode"
)

// cotTriggerRe: unambiguous CoT. "the user/the speaker" is verb-bound so a
// legitimate reply mentioning "the user manual" does not trip it. The trailing
// alternation is the snake_case-identifier addition (see file comment).
var cotTriggerRe = regexp.MustCompile(
	`(?i)(?:\bthe (?:user|speaker)s? (?:is|are|was|were|wants?|wanted|asks?` +
		`|asked|insists?|insisted|seems?|seemed|says?|said|repeats?|repeated` +
		`|claims?|claimed|mentions?|mentioned|requests?|requested|greets?|greeted` +
		`|needs?|needed)\b` +
		`|phrasing draft|delivery guidance|spoken delivery` +
		`|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b)`,
)

// cotSecondaryRe: meta terms a legit reply could contain (dev users ask the
// device about itself) — only dropped once CoT mode is already on.
var cotSecondaryRe = regexp.MustCompile(
	`(?i)(?:\bpersonas?\b|\bsystem prompts?\b|language lock|\baudio tags?\b` +
		`|\bemotion tool\b|via tool call` +
		`|\bmy search results?\b|\bsearch results? (?:show|suggest|indicate)\b` +
		`|\bsearch quer(?:y|ies)\b` +
		`|\b\d+ (?:sentences?|words)\b)`,
)

// cotLabelRe: all-ASCII label sentence ending in a colon ("Length Check:").
// Only meaningful when the reply language is NOT English.
var cotLabelRe = regexp.MustCompile(`^[\x20-\x7e]{1,40}:$`)

// cotOpenerRe: sentence-initial planning openers. Only meaningful when the
// reply language is NOT English.
var cotOpenerRe = regexp.MustCompile(
	`(?i)^\s*(?:users? (?:want|wants|is|are|asked|insists?)\b` +
		`|i (?:need|should|must|will|can(?:not|'t)?) \b` +
		`|therefore,? i\b|looking at the\b|re-examining\b|plan:\s*$)`,
)

const cotQuotes = "\"'“”‘’«»「」『』【】＂＇"

// Quoted spans inside a sentence don't decide its language: the leak corpus
// has English planning sentences that embed the reply language in quotes
// ("The search query 'cách dùng select trong itron os' didn't yield...") —
// with the quoted Vietnamese counted, the non-ASCII ratio calls the whole
// sentence non-English and the CoT line survives. RE2 has no lookarounds, so
// straight single-quote spans use boundary capture groups instead of the
// Python filter's (?<!\w)'…'(?!\w) — contractions ("didn't") can't open one.
var cotQuotedSpanRe = regexp.MustCompile(
	`"[^"]*"|“[^”]*”|‘[^’]*’|«[^»]*»|「[^」]*」|『[^』]*』`,
)

var cotSingleQuotedSpanRe = regexp.MustCompile(
	`(^|[^\pL\pN_])'[^']*'($|[^\pL\pN_])`,
)

func cotStripQuotedSpans(s string) string {
	s = cotQuotedSpanRe.ReplaceAllString(s, " ")
	return cotSingleQuotedSpanRe.ReplaceAllString(s, "$1 $2")
}

var cotNonASCIIRe = regexp.MustCompile(`[^\x00-\x7f]`)

// Leading audio/emotion tags like "[caring] " don't decide the language.
var cotLeadingTagsRe = regexp.MustCompile(`^(?:\s*\[[^\]]{1,30}\])+\s*`)

// CJK/Hangul/Kana have no spaces — tokenize per character so the fuzzy dedup
// works for Chinese/Japanese/Korean; everything else tokenizes per word.
// `[^\P{L}...]` = any letter that is not in the CJK ranges.
const cotCJKRange = `぀-ヿ㐀-䶿一-鿿豈-﫿가-힯`

var cotTokenRe = regexp.MustCompile(`[` + cotCJKRange + `]|[0-9]+|[^\P{L}` + cotCJKRange + `]+`)

var cotEnWordRe = regexp.MustCompile(`[A-Za-z]+`)

// For Latin-script non-English languages (French, German, Indonesian, ...) an
// ASCII-only sentence may BE the answer, so English detection additionally
// requires English function words.
var cotEnStopwords = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range strings.Fields(
		"the a an is are was were to of and or that this it its i you we they" +
			" will would should must need in on at with for be as by from since") {
		m[w] = true
	}
	return m
}()

// cotSplitSentences mirrors the Python _SENTENCE_SPLIT regex without
// lookarounds (RE2 has none): split after sentence enders (ASCII + fullwidth,
// colons included so planning labels separate from their payload) followed by
// whitespace, at newlines, and when an ASCII ender is glued straight onto an
// uppercase or non-ASCII start ("Finalize response.Khả năng..."). Decimals
// ("3.5") and lowercase glue ("Node.js") stay intact.
func cotSplitSentences(text string) []string {
	var out []string
	var cur strings.Builder
	runes := []rune(text)
	n := len(runes)
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
		}
	}
	for i := 0; i < n; i++ {
		r := runes[i]
		if r == '\n' {
			flush()
			continue
		}
		cur.WriteRune(r)
		if i+1 >= n {
			continue
		}
		next := runes[i+1]
		switch r {
		case '.', '!', '?', '。', '！', '？', '：', '；', ':':
			if unicode.IsSpace(next) {
				flush()
				for i+1 < n && unicode.IsSpace(runes[i+1]) {
					i++
				}
				continue
			}
		}
		switch r {
		case '.', '!', '?':
			if (next >= 'A' && next <= 'Z') || next > 0x7f {
				flush()
			}
		}
	}
	flush()
	return out
}

func cotWordSet(s string) map[string]struct{} {
	set := map[string]struct{}{}
	for _, t := range cotTokenRe.FindAllString(strings.ToLower(s), -1) {
		set[t] = struct{}{}
	}
	return set
}

func cotJaccard(a, b map[string]struct{}) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	inter := 0
	for t := range a {
		if _, ok := b[t]; ok {
			inter++
		}
	}
	return float64(inter) / float64(len(a)+len(b)-inter)
}

// cotLeakFilter is the per-turn stateful filter. Feed it reply text in
// arrival order; reuse one instance across a streamed prefix + remainder so
// CoT mode and the fuzzy-dedup memory carry over.
type cotLeakFilter struct {
	nonEnglish     bool
	nonASCIIScript bool
	cotMode        bool
	seen           []map[string]struct{}
	// dropped collects the sentences removed so far; callers decide how to
	// log them (the seed pass at lifecycle:end resets it to avoid re-logging
	// drops already reported at stream time).
	dropped []string
}

// newCoTLeakFilter builds a filter for the given config.json stt_language
// code ("vi", "en", "zh-CN", ...). "" is treated as English.
func newCoTLeakFilter(langCode string) *cotLeakFilter {
	code := strings.ToLower(strings.TrimSpace(langCode))
	primary := code
	if i := strings.IndexAny(primary, "-_"); i > 0 {
		primary = primary[:i]
	}
	f := &cotLeakFilter{
		nonEnglish: code != "" && primary != "en",
	}
	// Languages whose real answers are dense in non-ASCII letters — there,
	// any ASCII-only multi-word sentence inside CoT mode is safely English.
	switch primary {
	case "vi", "zh", "ja", "ko", "th":
		f.nonASCIIScript = true
	}
	return f
}

func (f *cotLeakFilter) looksEnglish(sentence string) bool {
	s := strings.TrimSpace(cotLeadingTagsRe.ReplaceAllString(sentence, ""))
	// Judge the sentence by its own voice, not by what it quotes: quoted
	// reply-language text inside an English planning sentence must not
	// rescue it.
	s = cotStripQuotedSpans(s)
	letters, nonASCII := 0, 0
	for _, r := range s {
		if unicode.IsLetter(r) {
			letters++
			if r > 127 {
				nonASCII++
			}
		}
	}
	if letters == 0 {
		return false
	}
	// Ratio, not any(): "non-cliché" has one é but is English planning text,
	// while real Vietnamese runs ~30%+ diacritics and CJK ~100%.
	if float64(nonASCII)/float64(letters) > 0.05 {
		return false
	}
	words := cotEnWordRe.FindAllString(s, -1)
	if len(words) < 3 {
		// Bare interjections ("OK!", "Ha ha") survive.
		return false
	}
	if f.nonASCIIScript {
		return true
	}
	stop := 0
	for _, w := range words {
		if cotEnStopwords[strings.ToLower(w)] {
			stop++
		}
	}
	return stop >= 2
}

func (f *cotLeakFilter) isLeak(sentence string) bool {
	s := strings.TrimSpace(sentence)
	if s == "" {
		return false
	}
	if cotTriggerRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if f.nonEnglish && cotOpenerRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if f.nonEnglish && cotLabelRe.MatchString(s) {
		f.cotMode = true
		return true
	}
	if !f.cotMode {
		return false
	}
	if cotSecondaryRe.MatchString(s) {
		return true
	}
	if f.nonEnglish && f.looksEnglish(s) {
		return true
	}
	// Leaked turns carry the answer as a QUOTED phrasing draft before the
	// real thing — drop the quoted draft too or the answer is spoken twice.
	runes := []rune(s)
	if strings.ContainsRune(cotQuotes, runes[0]) || strings.ContainsRune(cotQuotes, runes[len(runes)-1]) {
		return true
	}
	// Bare plan fragments ("1.", "Stop.", "Finalize response.") — ASCII runts
	// up to two tokens. Pure audio tags ("[confused]") are exempt: they steer
	// TTS delivery, not content.
	bare := strings.TrimSpace(cotLeadingTagsRe.ReplaceAllString(s, ""))
	if bare != "" && !cotNonASCIIRe.MatchString(bare) && len(cotWordSet(bare)) <= 2 {
		return true
	}
	// Fuzzy near-duplicate of a sentence already kept this turn — drafts
	// differ from the final answer by a word or two, so exact dedup misses.
	words := cotWordSet(s)
	if len(words) > 0 {
		for _, seen := range f.seen {
			if cotJaccard(words, seen) >= 0.7 {
				return true
			}
		}
	}
	return false
}

// filterText drops CoT sentences from text, keeping the rest in order.
// Dropped sentences accumulate in f.dropped for the caller to log.
func (f *cotLeakFilter) filterText(text string) string {
	if text == "" {
		return text
	}
	var kept []string
	for _, sentence := range cotSplitSentences(text) {
		if f.isLeak(sentence) {
			f.dropped = append(f.dropped, strings.TrimSpace(sentence))
			continue
		}
		s := strings.TrimSpace(sentence)
		if s == "" {
			continue
		}
		kept = append(kept, s)
		if ws := cotWordSet(s); len(ws) > 0 {
			f.seen = append(f.seen, ws)
		}
	}
	return strings.Join(kept, " ")
}

// cotDroppedPreview joins dropped sentences into a bounded preview for logs.
func cotDroppedPreview(dropped []string, max int) string {
	joined := strings.Join(dropped, " | ")
	if len(joined) > max {
		return joined[:max] + "…"
	}
	return joined
}
