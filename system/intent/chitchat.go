// Chitchat matching — exact-match greetings / farewells / thanks across
// vi/en/zh, checked before the command rules so a bare "chào" / "hi" / "你好"
// answers from the WAV cache instead of the LLM.
package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/lib/i18n"
)

// chitchatRule is the local metadata for one chitchat intent. Input
// phrases (per lang) and reply variants (per lang) both live in i18n —
// look up via i18n.InputPhrases(reply) and i18n.PickIn(reply, lang).
type chitchatRule struct {
	reply   i18n.Phrase // i18n key — input matchers + reply variants both keyed by this
	intent  string      // "greeting" / "farewell" / "thanks" — for log/Rule field
	emotion string      // emotion fired alongside reply
}

// Order matters — a longer utterance can contain a short phrase, so specific
// intents (presence_check, apology, compliment) must run before broad ones
// (greeting/farewell).
// Nevermind goes last because its trigger words (e.g. "thôi") are short and
// would shadow other intents that include the same token in their pool.
var chitchatRules = []chitchatRule{
	{reply: i18n.PhraseChitchatPresenceCheck, intent: "presence_check", emotion: "happy"},
	{reply: i18n.PhraseChitchatApology, intent: "apology", emotion: "happy"},
	{reply: i18n.PhraseChitchatCompliment, intent: "compliment", emotion: "happy"},
	{reply: i18n.PhraseChitchatGreeting, intent: "greeting", emotion: "happy"},
	{reply: i18n.PhraseChitchatFarewell, intent: "farewell", emotion: "happy"},
	{reply: i18n.PhraseChitchatThanks, intent: "thanks", emotion: "happy"},
	{reply: i18n.PhraseChitchatNevermind, intent: "nevermind", emotion: "idle"},
}

// matchChitchat returns a Result when text starts with a chitchat phrase in
// any supported language AND looks short/social (≤5 words, no command verbs).
// Reply is picked in the matched-input language so "hi" → English reply,
// "chào" → Vietnamese reply — keeps the agent on the user's current language
// regardless of configured i18n.Lang().
func matchChitchat(text string) *Result {
	if text == "" {
		return nil
	}
	t := strings.ToLower(strings.TrimSpace(text))
	t = strings.TrimRight(t, ".!?,。！？，")

	// Strip leading wake word so "<name> xin chào" → "xin chào", "<name> cảm
	// ơn" → "cảm ơn". Bare wake-word / "<name> ơi" → "" → user is just
	// calling the device by name; short-circuit with a greeting reply.
	t = stripWakeWord(t)
	if t == "" {
		return bareAttentionResult()
	}

	// Length gate: greeting/farewell/thanks are short. "Chào <name> hôm nay
	// bạn thế nào" → 6 words → fall through to LLM so context isn't lost.
	// Word counting on bytes works for VN/EN; for ZH treat each rune as a
	// word since CJK has no spaces.
	if wordCountLoose(t) > 5 {
		return nil
	}

	// Reject if any command word is present — the user is asking for an
	// action and OpenClaw / the command rules must see it.
	for _, w := range i18n.ChitchatCommandWords() {
		if strings.Contains(t, w) {
			return nil
		}
	}

	for _, r := range chitchatRules {
		for lang, phrases := range i18n.InputPhrases(r.reply) {
			for _, p := range phrases {
				// Whole-phrase match, NOT a substring: "hi" sits inside
				// "this", "his", "machine", so a plain Contains answered
				// "What is this?" and "Body of his arm." as greetings and
				// swallowed the turn before the agent ever saw it.
				if !containsPhrase(t, p) {
					continue
				}
				reply := i18n.PickIn(r.reply, lang)
				if reply == "" {
					continue
				}
				postEmotion(fmt.Sprintf(`{"emotion":"%s","intensity":0.7}`, r.emotion))
				return &Result{
					TTSText: reply,
					Emotion: r.emotion,
					Rule:    "chitchat_" + r.intent,
					Actions: []string{"POST /emotion " + r.emotion},
				}
			}
		}
	}
	return nil
}

// wordCountLoose counts space-separated tokens for VN/EN. For CJK text
// (Chinese), space-split returns 1 since there are no spaces — fall back
// to rune count (each char ≈ a "word" for the purpose of "is this short").
func wordCountLoose(s string) int {
	fields := strings.Fields(s)
	if len(fields) > 1 {
		return len(fields)
	}
	// Single field — could be EN/VN one word or CJK run with no spaces.
	// Count runes if any non-ASCII rune is present (CJK heuristic).
	for _, r := range s {
		if r > 127 {
			n := 0
			for range s {
				n++
			}
			// Round down by dividing by 2 — typical Chinese phrase has
			// 2 runes per "word" (e.g. 你好 = 1 social word).
			if n/2 < 1 {
				return 1
			}
			return n / 2
		}
	}
	return len(fields)
}

// stripChitchatPrefixes removes the sensing-message envelope around the
// user's actual words so an exact-match chitchat rule can fire. The sensing
// path wraps voice text like:
//
//	[user] [ambient] Unknown Speaker: [voice:voice_46] chào (audio saved at /tmp/...)
//
// Stripping leading [tag]…[tag] groups, the speaker label up to the first
// colon, the [voice:…] tag after it, and the trailing (audio saved …) note
// leaves just "chào" which can match the chitchat table.
func stripChitchatPrefixes(s string) string {
	s = strings.TrimSpace(s)
	// Strip leading [tag] groups.
	for strings.HasPrefix(s, "[") {
		end := strings.Index(s, "]")
		if end < 0 {
			break
		}
		s = strings.TrimSpace(s[end+1:])
	}
	// Strip "Speaker - Name:" / "Unknown Speaker:" prefix when colon is
	// near the start (avoid eating user text that happens to contain ":").
	if idx := strings.Index(s, ":"); idx >= 0 && idx < 40 {
		before := strings.ToLower(s[:idx])
		if strings.Contains(before, "speaker") {
			s = strings.TrimSpace(s[idx+1:])
		}
	}
	// Strip another round of leading [voice:…] tags after the speaker label.
	for strings.HasPrefix(s, "[") {
		end := strings.Index(s, "]")
		if end < 0 {
			break
		}
		s = strings.TrimSpace(s[end+1:])
	}
	// Strip trailing "(audio saved at …)" / "(audio is too short …)" — our
	// own annotation, never user content. Anything else in parens stays.
	if idx := strings.LastIndex(s, "("); idx > 0 {
		rest := s[idx:]
		if strings.Contains(rest, "audio saved") || strings.Contains(rest, "audio is too short") {
			s = strings.TrimSpace(s[:idx])
		}
	}
	return s
}

// stripWakeWord removes a leading wake-word token (the device name, "<name>
// ơi", …) from already-lowercased chitchat input. Boundary check ensures a
// longer token with the name as a prefix isn't accidentally stripped — must be
// followed by whitespace, comma, punctuation, or end-of-string. The wake-word
// list is kept longest-first by i18n.ChitchatWakeWords so "<name> ơi xin chào"
// strips the compound form rather than just "<name>", which would leave a
// dangling "ơi" that matches no rule.
func stripWakeWord(s string) string {
	for _, w := range i18n.ChitchatWakeWords() {
		if !strings.HasPrefix(s, w) {
			continue
		}
		rest := s[len(w):]
		if rest == "" {
			return ""
		}
		c := rest[0]
		if c == ' ' || c == ',' || c == '.' || c == '!' || c == '?' {
			return strings.TrimSpace(strings.TrimLeft(rest, " ,.!?"))
		}
	}
	return s
}

// bareAttentionResult fires when the user said only the wake word (the device
// name, "<name> ơi"). Replies with a greeting in the configured language —
// skipping LLM RT keeps the device responsive when the user is just calling.
func bareAttentionResult() *Result {
	reply := i18n.Pick(i18n.PhraseChitchatGreeting)
	if reply == "" {
		return nil
	}
	postEmotion(`{"emotion":"happy","intensity":0.7}`)
	return &Result{
		TTSText: reply,
		Emotion: "happy",
		Rule:    "chitchat_attention",
		Actions: []string{"POST /emotion happy"},
	}
}
