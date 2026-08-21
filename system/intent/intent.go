// Package intent provides local intent matching for common voice commands.
// Matched commands execute directly against HAL APIs, bypassing OpenClaw
// for instant response (~50ms vs ~3-5s through the agent pipeline).
//
// Layout (one file per function group, all one package):
//   - intent.go         — engine: Result, Match, capability gate, shared helpers
//   - chitchat.go       — exact-match greetings/farewells/thanks (vi/en/zh)
//   - rules_led.go      — LED color / on / off / dim
//   - rules_scene.go    — scene activation + scene off
//   - rules_audio.go    — volume, mute, music stop, TTS stop
//   - rules_misc.go     — what-time and other hardware-free rules
//   - rules_tracking.go — servo vision tracking
package intent

import (
	"log/slog"
	"slices"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// Result holds what to do after a match: the HAL action + a TTS reply.
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

type rule struct {
	name  string
	match func(string) bool
	exec  func(string) *Result
	// capability gates the rule to devices that declare it in ROBOT.md (e.g.
	// "motion" for servo tracking). Empty = no hardware dependency, always on.
	capability string
}

// Checked in order — first match wins. Grouped by function into rules_*.go;
// ordering constraints (led_color before generic led on/off, scene_off before
// scene activation) live inside each group, and the group order below
// preserves the original single-table order.
var rules = slices.Concat(ledRules, sceneRules, audioRules, miscRules, trackingRules)

// Match tries to match a voice command to a local intent. Returns nil if no
// match — caller should fall through to OpenClaw. Chitchat (exact-match
// greetings/farewells/thanks across vi/en/zh) is checked first so a bare
// "chào" / "hi" / "你好" hits the WAV cache in ~50ms instead of the 8s LLM TTFT.
//
// Rules that drive a peripheral are gated by the device's declared capabilities
// (set once via Configure): a body without that hardware (e.g. intern-v2 has no
// servo) never matches a command it can't execute, and never POSTs to a HAL
// route its body doesn't serve.
func Match(text string) *Result {
	return match(text, chitchatEnabled())
}

// MatchCommands is Match with chitchat skipped — only the command rules run.
// Use it when something else already owns social talk (see SetChitchatEnabled).
func MatchCommands(text string) *Result {
	return match(text, false)
}

func match(text string, allowChitchat bool) *Result {
	// Chitchat needs a stricter normalization than command rules — speaker
	// prefixes, voice tags, and the (audio saved at ...) suffix from the
	// sensing message must be stripped for an exact phrase match to work.
	if allowChitchat {
		if r := matchChitchat(stripChitchatPrefixes(text)); r != nil {
			return r
		}
	}

	t := normalize(text)
	for _, r := range rules {
		if !capEnabled(r.capability) {
			continue
		}
		if r.match(t) {
			res := r.exec(t)
			res.Rule = r.name
			return res
		}
	}
	return nil
}

// deviceCaps is the device's declared capability set (ROBOT.md), set once at
// startup via Configure and read-only after. It gates which command rules run
// and which HAL peripherals local intents drive. nil = fail-open (all rules),
// matching legacy single-device behavior.
var deviceCaps map[string]bool

// Configure sets the capability set used to gate local intents. Call once at
// startup before any Match. nil/empty caps = fail-open.
func Configure(caps map[string]bool) { deviceCaps = caps }

// chitchatOff turns off the social rules while another layer owns social talk.
// Guarded by chitchatMu because the config watcher flips it at runtime.
var (
	chitchatMu  sync.RWMutex
	chitchatOff bool
)

// SetChitchatEnabled turns the chitchat rules on or off.
//
// They exist to answer "hi" / "chào" from the WAV cache instead of paying an
// LLM round-trip. The realtime voice agent answers social talk itself, in
// under a second and in character, and it gets every voice turn BEFORE
// os-server does — so with realtime on, chitchat only ever fires on turns the
// model stayed silent for, where it barges in with a canned line in a
// different voice. Off is the right default there; the command rules (lights,
// volume, time) stay on either way since those genuinely beat the model.
func SetChitchatEnabled(enabled bool) {
	chitchatMu.Lock()
	chitchatOff = !enabled
	chitchatMu.Unlock()
}

func chitchatEnabled() bool {
	chitchatMu.RLock()
	defer chitchatMu.RUnlock()
	return !chitchatOff
}

// capEnabled is fail-open: an empty capability (no hardware dependency) is always
// on; otherwise nil/empty deviceCaps → true. The maximal reference device (Lamp)
// declares every capability, so it keeps every rule.
func capEnabled(capability string) bool {
	if capability == "" {
		return true
	}
	return len(deviceCaps) == 0 || deviceCaps[capability]
}

// CacheableReplies is the set of intent reply phrases that should be
// pre-rendered into the hal WAV cache at boot. Listed here (and not
// derived from the rules table) because rule.exec is dynamic — some
// replies depend on runtime input (color name, current time) and aren't
// suitable for caching.
var CacheableReplies = func() []string {
	out := []string{
		"Light on!", "Light off!", "Back to normal!", "Goodnight!",
		"Volume up!", "Volume down!", "Music stopped.", "Dimmed.", "Max brightness!",
		"Speaker on!",
	}
	// Pull every chitchat reply variant from i18n so the WAV cache covers
	// them after reboot — first call is then ~50ms playback instead of 1.5s
	// ElevenLabs render.
	for _, r := range chitchatRules {
		out = append(out, i18n.AllVariantsAcrossLangs(r.reply)...)
	}
	return out
}()

// --- shared helpers ---

func normalize(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

func anyOf(keywords ...string) func(string) bool {
	return func(t string) bool {
		for _, kw := range keywords {
			if containsPhrase(t, kw) {
				return true
			}
		}
		return false
	}
}

// containsPhrase reports whether kw occurs in t as a whole phrase: the
// occurrence must not sit inside a larger word, so "unmute speaker" does
// NOT match keyword "mute speaker". Boundaries are non-alphanumeric ASCII;
// multibyte (Vietnamese/Chinese) neighbors count as boundaries, which is
// correct since keywords are English-only.
func containsPhrase(t, kw string) bool {
	for i := 0; ; {
		j := strings.Index(t[i:], kw)
		if j < 0 {
			return false
		}
		start := i + j
		end := start + len(kw)
		if (start == 0 || !isASCIIWordChar(t[start-1])) &&
			(end == len(t) || !isASCIIWordChar(t[end])) {
			return true
		}
		i = start + 1
	}
}

func isASCIIWordChar(b byte) bool {
	return b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9'
}

// pickRandom returns a pseudo-random pick using the current time. Avoids
// pulling in math/rand state for low-stakes variance.
func pickRandom(opts []string) string {
	if len(opts) == 0 {
		return ""
	}
	return opts[int(time.Now().UnixNano())%len(opts)]
}

func post(path, body string) {
	if err := hal.PostRaw(path, body); err != nil {
		slog.Warn("[intent] hal call failed", "path", path, "error", err)
	}
}

// postEmotion drives an emotion expression, but only on a body that can show one
// (declares the `expression` capability — a screen, LED, or servo to express
// through). Emotion is cross-cutting (fired as a flourish by several command/
// chitchat rules), so the guard lives here rather than on each rule's capability.
// Fail-open via capEnabled.
func postEmotion(body string) {
	if capEnabled(device.CapExpression) {
		post("/emotion", body)
	}
}
