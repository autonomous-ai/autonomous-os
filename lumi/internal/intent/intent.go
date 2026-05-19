// Package intent provides local intent matching for common voice commands.
// Matched commands execute directly against LeLamp APIs, bypassing OpenClaw
// for instant response (~50ms vs ~3-5s through the agent pipeline).
package intent

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/lib/lelamp"
)

// Result holds what to do after a match: the LeLamp action + a TTS reply.
type Result struct {
	// TTSText is spoken back to the user via /voice/speak.
	TTSText string
	// LEDChanged is true when this intent sets an LED color/scene (locks ambient breathing).
	LEDChanged bool
	// LEDOff is true when this intent turns the LED off (unlocks ambient breathing).
	LEDOff bool
	// Emotion is the emotion name if this intent triggered an /emotion call.
	Emotion string
	// Rule is the name of the matched rule for debugging.
	Rule string
	// Actions lists hardware API calls made during exec (e.g. "POST /led/solid", "POST /emotion").
	Actions []string
}

// Match tries to match a voice command to a local intent.
// Returns nil if no match — caller should fall through to OpenClaw.
// Chitchat (exact-match greetings/farewells/thanks across vi/en/zh) is
// checked first so a bare "chào" / "hi" / "你好" hits the WAV cache in
// ~50ms instead of going through the 8s LLM TTFT.
func Match(text string) *Result {
	// Chitchat needs a stricter normalization than command rules — speaker
	// prefixes, voice tags, and the (audio saved at ...) suffix from the
	// sensing message must be stripped for an exact phrase match to work.
	if r := matchChitchat(stripChitchatPrefixes(text)); r != nil {
		return r
	}

	t := normalize(text)
	for _, r := range rules {
		if r.match(t) {
			res := r.exec(t)
			res.Rule = r.name
			return res
		}
	}
	return nil
}

// CacheableReplies is the set of intent reply phrases that should be
// pre-rendered into the lelamp WAV cache at boot. Listed here (and not
// derived from the rules table) because rule.exec is dynamic — some
// replies depend on runtime input (color name, current time) and aren't
// suitable for caching.
var CacheableReplies = func() []string {
	out := []string{
		"Light on!", "Light off!", "Back to normal!", "Goodnight!",
		"Volume up!", "Volume down!", "Music stopped.", "Dimmed.", "Max brightness!",
	}
	// Pull every chitchat reply variant from i18n so the WAV cache covers
	// them after reboot — first call is then ~50ms playback instead of 1.5s
	// ElevenLabs render.
	for _, r := range chitchatRules {
		out = append(out, i18n.AllVariantsAcrossLangs(r.reply)...)
	}
	return out
}()

// --- chitchat (greetings / farewells / thanks) ---

// chitchatRule is the local metadata for one chitchat intent. Input
// phrases (per lang) and reply variants (per lang) both live in i18n —
// look up via i18n.InputPhrases(reply) and i18n.PickIn(reply, lang).
type chitchatRule struct {
	reply   i18n.Phrase // i18n key — input matchers + reply variants both keyed by this
	intent  string      // "greeting" / "farewell" / "thanks" — for log/Rule field
	emotion string      // emotion fired alongside reply
}

// Order matters — Contains is greedy so specific phrases (presence_check,
// apology, compliment) must run before broad ones (greeting/farewell).
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
// "chào" → Vietnamese reply — keeps the lamp on the user's current language
// regardless of configured i18n.Lang().
func matchChitchat(text string) *Result {
	if text == "" {
		return nil
	}
	t := strings.ToLower(strings.TrimSpace(text))
	t = strings.TrimRight(t, ".!?,。！？，")

	// Strip leading wake word so "Lumi xin chào" → "xin chào", "Lami cảm
	// ơn" → "cảm ơn". Bare wake-word / "lumi ơi" → "" → user is just
	// calling Lumi by name; short-circuit with a greeting reply.
	t = stripWakeWord(t)
	if t == "" {
		return bareAttentionResult()
	}

	// Length gate: greeting/farewell/thanks are short. "Chào Lumi hôm nay
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
				// Substring match — exact / prefix / suffix all hit. The
				// length gate above (≤5 words) and command-verb reject
				// already bound false positives, so Contains is safe and
				// catches real speech variation: "chào nha", "lumi chào
				// em", "cảm ơn rất nhiều", "thanks man", etc.
				if !strings.Contains(t, p) {
					continue
				}
				reply := i18n.PickIn(r.reply, lang)
				if reply == "" {
					continue
				}
				post("/emotion", fmt.Sprintf(`{"emotion":"%s","intensity":0.7}`, r.emotion))
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

// stripWakeWord removes a leading wake-word token ("lumi", "làmi", "lumi
// ơi", …) from already-lowercased chitchat input. Boundary check ensures
// "luminous" / "lumière" aren't accidentally stripped — must be followed by
// whitespace, comma, punctuation, or end-of-string. The wake-word list is
// kept longest-first by i18n.ChitchatWakeWords so "lumi ơi xin chào" strips
// the compound form rather than just "lumi", which would leave a dangling
// "ơi" that matches no rule.
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

// bareAttentionResult fires when the user said only the wake word ("Lumi",
// "Lumi ơi", "Lami"). Replies with a greeting in the configured language —
// skipping LLM RT keeps the lamp responsive when the user is just calling.
func bareAttentionResult() *Result {
	reply := i18n.Pick(i18n.PhraseChitchatGreeting)
	if reply == "" {
		return nil
	}
	post("/emotion", `{"emotion":"happy","intensity":0.7}`)
	return &Result{
		TTSText: reply,
		Emotion: "happy",
		Rule:    "chitchat_attention",
		Actions: []string{"POST /emotion happy"},
	}
}

// pickRandom returns a pseudo-random pick using the current time. Avoids
// pulling in math/rand state for low-stakes variance.
func pickRandom(opts []string) string {
	if len(opts) == 0 {
		return ""
	}
	return opts[int(time.Now().UnixNano())%len(opts)]
}

// --- rules table ---

type rule struct {
	name  string
	match func(string) bool
	exec  func(string) *Result
}

// colorKeywords maps color keywords to RGB values.
// Checked in order — first match wins.
var colorKeywords = []struct {
	keywords []string
	rgb      [3]int
	name     string
}{
	{[]string{"yellow"}, [3]int{255, 220, 0}, "Yellow"},
	{[]string{"red"}, [3]int{255, 0, 0}, "Red"},
	{[]string{"green"}, [3]int{0, 200, 100}, "Green"},
	{[]string{"blue"}, [3]int{0, 100, 255}, "Blue"},
	{[]string{"cyan"}, [3]int{0, 200, 150}, "Cyan"},
	{[]string{"purple", "violet"}, [3]int{100, 50, 200}, "Purple"},
	{[]string{"orange"}, [3]int{255, 100, 0}, "Orange"},
	{[]string{"pink"}, [3]int{255, 80, 150}, "Pink"},
	{[]string{"white"}, [3]int{255, 255, 255}, "White"},
	{[]string{"warm"}, [3]int{255, 180, 100}, "Warm"},
}

// extractColor returns the RGB and name for the first color keyword found in t.
func extractColor(t string) ([3]int, string, bool) {
	for _, c := range colorKeywords {
		for _, kw := range c.keywords {
			if strings.Contains(t, kw) {
				return c.rgb, c.name, true
			}
		}
	}
	return [3]int{}, "", false
}

// isLEDOnCommand returns true if t contains a "turn on light" trigger phrase.
func isLEDOnCommand(t string) bool {
	triggers := []string{"turn on the light", "light on", "set color", "change color", "set the light"}
	for _, kw := range triggers {
		if strings.Contains(t, kw) {
			return true
		}
	}
	return false
}

var rules = []rule{
	// --- LED color (must be before generic LED on/off) ---
	{
		name: "led_color",
		match: func(t string) bool {
			if !isLEDOnCommand(t) {
				return false
			}
			_, _, ok := extractColor(t)
			return ok
		},
		exec: func(t string) *Result {
			rgb, name, _ := extractColor(t)
			post("/led/effect/stop", "")
			body := fmt.Sprintf(`{"color":[%d,%d,%d]}`, rgb[0], rgb[1], rgb[2])
			post("/led/solid", body)
			return &Result{TTSText: name + " light on!", LEDChanged: true, Actions: []string{"POST /led/effect/stop", "POST /led/solid " + body}}
		},
	},

	// --- LED on/off ---
	{
		name:  "led_on",
		match: anyOf("turn on the light", "light on"),
		exec: func(string) *Result {
			post("/led/solid", `{"color":[255,220,180]}`)
			post("/emotion", `{"emotion":"happy","intensity":0.6}`)
			return &Result{TTSText: "Light on!", LEDChanged: true, Actions: []string{`POST /led/solid {"color":[255,220,180]}`, `POST /emotion {"emotion":"happy","intensity":0.6}`}}
		},
	},
	{
		name:  "led_off",
		match: anyOf("turn off the light", "light off"),
		exec: func(string) *Result {
			post("/led/off", "")
			post("/emotion", `{"emotion":"idle","intensity":0.3}`)
			return &Result{TTSText: "Light off!", LEDOff: true, Actions: []string{"POST /led/off", `POST /emotion {"emotion":"idle","intensity":0.3}`}}
		},
	},

	// --- Scene off (must be before scene activation rules) ---
	{
		name: "scene_off",
		match: func(t string) bool {
			return (strings.Contains(t, "turn off") || strings.Contains(t, "disable")) &&
				(strings.Contains(t, "mode") || strings.Contains(t, "scene"))
		},
		exec: func(string) *Result {
			post("/scene/off", "")
			return &Result{TTSText: "Back to normal!", LEDOff: true, Actions: []string{"POST /scene/off"}}
		},
	},

	// --- Scenes ---
	{
		name:  "scene_reading",
		match: anyOf("reading mode", "reading light"),
		exec:  sceneExec("reading", "Reading mode!"),
	},
	{
		name:  "scene_focus",
		match: anyOf("focus mode", "focus light"),
		exec:  sceneExec("focus", "Focus mode!"),
	},
	{
		name:  "scene_relax",
		match: anyOf("relax mode", "relax light"),
		exec:  sceneExec("relax", "Relax mode!"),
	},
	{
		name:  "scene_movie",
		match: anyOf("movie mode", "movie light"),
		exec:  sceneExec("movie", "Movie mode!"),
	},
	{
		name:  "scene_night",
		match: anyOf("goodnight", "good night", "night mode"),
		exec: func(string) *Result {
			post("/scene", `{"scene":"night"}`)
			post("/emotion", `{"emotion":"sleepy","intensity":0.4}`)
			return &Result{TTSText: "Goodnight!", LEDChanged: true, Actions: []string{`POST /scene {"scene":"night"}`, `POST /emotion {"emotion":"sleepy","intensity":0.4}`}}
		},
	},
	{
		name:  "scene_energize",
		match: anyOf("brighter", "energize", "max brightness"),
		exec:  sceneExec("energize", "Max brightness!"),
	},

	// --- Volume ---
	{
		name:  "volume_up",
		match: anyOf("volume up", "louder"),
		exec: func(string) *Result {
			post("/audio/volume", `{"volume":100}`)
			return &Result{TTSText: "Volume up!", Actions: []string{`POST /audio/volume {"volume":80}`}}
		},
	},
	{
		name:  "volume_down",
		match: anyOf("volume down", "quieter"),
		exec: func(string) *Result {
			post("/audio/volume", `{"volume":30}`)
			return &Result{TTSText: "Volume down!", Actions: []string{`POST /audio/volume {"volume":30}`}}
		},
	},
	{
		name:  "mute_speaker",
		match: anyOf("mute speaker", "mute the speaker"),
		exec: func(string) *Result {
			post("/speaker/mute", "")
			return &Result{TTSText: "", Actions: []string{`POST /speaker/mute`}}
		},
	},

	// --- Music control ---
	{
		name:  "music_stop",
		match: anyOf("stop music", "stop the music", "music off", "stop playing"),
		exec: func(string) *Result {
			post("/audio/stop", "")
			return &Result{TTSText: "Music stopped.", Actions: []string{"POST /audio/stop"}}
		},
	},

	// --- TTS stop (interrupt Lumi speaking) ---
	{
		name:  "stop_talking",
		match: anyOf("stop talking", "ok stop"),
		exec: func(string) *Result {
			post("/tts/stop", "")
			return &Result{TTSText: "", Actions: []string{"POST /tts/stop"}}
		},
	},

	// --- Time ---
	{
		name:  "what_time",
		match: anyOf("what time", "whats the time", "what's the time"),
		exec: func(string) *Result {
			now := time.Now()
			text := fmt.Sprintf("It's %s.", now.Format("3:04 PM"))
			return &Result{TTSText: text, Actions: []string{"time.Now()"}}
		},
	},

	// --- Dim / brightness ---
	{
		name:  "dim",
		match: anyOf("dim the light", "dimmer", "dim light"),
		exec: func(string) *Result {
			post("/led/solid", `{"color":[80,60,40]}`)
			return &Result{TTSText: "Dimmed.", LEDChanged: true, Actions: []string{`POST /led/solid {"color":[80,60,40]}`}}
		},
	},

	// --- Vision tracking ---
	// Going through OpenClaw costs ~3-5s per "track the cup" command. Local
	// match → direct POST /servo/track keeps the latency under ~100ms so the
	// camera starts following before the user has finished their next breath.
	{
		name:  "servo_track_stop",
		match: anyOf("stop tracking", "stop following", "stop watching", "stop track"),
		exec: func(string) *Result {
			post("/servo/track/stop", "")
			return &Result{TTSText: "Stopped tracking.", Actions: []string{"POST /servo/track/stop"}}
		},
	},
	{
		name: "servo_track",
		match: func(t string) bool {
			if !hasTrackVerb(t) {
				return false
			}
			return extractTrackTarget(t) != ""
		},
		exec: func(t string) *Result {
			target := extractTrackTarget(t)
			body := fmt.Sprintf(`{"target":["%s"]}`, target)
			post("/servo/track", body)
			return &Result{
				TTSText: fmt.Sprintf("Tracking %s.", target),
				Actions: []string{"POST /servo/track " + body},
			}
		},
	},
}

// hasTrackVerb returns true when the command contains a tracking verb. Phrased
// to require the verb early in the sentence so "I'd like to follow up on..." or
// "I can't watch that movie" don't trigger the camera.
func hasTrackVerb(t string) bool {
	for _, kw := range []string{"track ", "follow ", "watch "} {
		if strings.HasPrefix(t, kw) || strings.Contains(t, " "+kw) {
			return true
		}
	}
	return false
}

// trackTargets maps spoken nouns to the label sent to /servo/track. Pronouns
// resolve to "person"; ambiguous words ("me", "yourself") are explicit so a
// stray "watch yourself" still picks the right target.
var trackTargets = []struct {
	keywords []string
	label    string
}{
	{[]string{"face", "my face", "the face"}, "face"},
	{[]string{"hand", "my hand", "the hand"}, "hand"},
	{[]string{"me", "myself", "user", "us"}, "person"},
	{[]string{"person", "people", "human", "man", "woman", "the guy", "that guy"}, "person"},
	{[]string{"dog"}, "dog"},
	{[]string{"cat"}, "cat"},
	{[]string{"bird"}, "bird"},
	{[]string{"cup", "mug", "coffee cup"}, "cup"},
	{[]string{"bottle", "water bottle"}, "bottle"},
	{[]string{"phone", "smartphone", "cell phone", "mobile"}, "cell phone"},
	{[]string{"book"}, "book"},
	{[]string{"remote", "tv remote"}, "remote"},
	{[]string{"laptop", "computer"}, "laptop"},
	{[]string{"keyboard"}, "keyboard"},
	{[]string{"mouse"}, "mouse"},
	{[]string{"teddy", "teddy bear", "stuffed animal"}, "teddy bear"},
	{[]string{"ball"}, "sports ball"},
	{[]string{"backpack", "bag"}, "backpack"},
	{[]string{"chair"}, "chair"},
	{[]string{"clock"}, "clock"},
	{[]string{"scissors"}, "scissors"},
	{[]string{"banana"}, "banana"},
	{[]string{"apple"}, "apple"},
	{[]string{"orange"}, "orange"},
}

// extractTrackTarget pulls the COCO/face label from a tracking command. Returns
// "" when nothing matches — caller should then fall through to OpenClaw which
// can use YOLOWorld open-vocab for less common nouns.
func extractTrackTarget(t string) string {
	for _, e := range trackTargets {
		for _, kw := range e.keywords {
			if strings.Contains(t, kw) {
				return e.label
			}
		}
	}
	return ""
}

// --- helpers ---

func normalize(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

func anyOf(keywords ...string) func(string) bool {
	return func(t string) bool {
		for _, kw := range keywords {
			if strings.Contains(t, kw) {
				return true
			}
		}
		return false
	}
}

func sceneExec(scene, reply string) func(string) *Result {
	return func(string) *Result {
		body := fmt.Sprintf(`{"scene":"%s"}`, scene)
		post("/scene", body)
		return &Result{TTSText: reply, LEDChanged: true, Actions: []string{"POST /scene " + body}}
	}
}

func emotionExec(emotion, reply string) func(string) *Result {
	return func(string) *Result {
		body := fmt.Sprintf(`{"emotion":"%s","intensity":0.8}`, emotion)
		post("/emotion", body)
		return &Result{TTSText: reply, Emotion: emotion, Actions: []string{"POST /emotion " + body}}
	}
}

func post(path, body string) {
	if err := lelamp.PostRaw(path, body); err != nil {
		slog.Warn("[intent] lelamp call failed", "path", path, "error", err)
	}
}
