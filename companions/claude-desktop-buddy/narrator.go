package main

import (
	"log"
	"sync"
)

// Narrator turns Claude Desktop activity into short TTS announcements
// (UC-9). It does not read the assistant's reply content — just emits
// per-state-change / per-tool-use markers like "Searching the web" or
// "Done" so the user can keep their attention on the Mac.
//
// Throttle policy: each category fires at most once per "turn". A turn
// starts when a new user message arrives (or when the state machine
// transitions back into busy from idle). This keeps the narrator from
// blurting "Thinking" three times when Claude does multiple thinking
// blocks in a single reply.
type Narrator struct {
	lang  string
	speak func(text string)

	mu       sync.Mutex
	turnSeen map[NarrationCategory]bool
}

// NewNarrator constructs a narrator bound to a single TTS sink.
// Passing speak=nil disables narration (useful when LeLamp is offline
// or the operator wants to mute announcements without ripping out the
// wiring). The language is clamped to a supported one so unknown
// values fall back to English rather than emitting raw category ids.
func NewNarrator(lang string, speak func(text string)) *Narrator {
	return &Narrator{
		lang:     supportedLang(lang),
		speak:    speak,
		turnSeen: make(map[NarrationCategory]bool),
	}
}

// StartTurn resets the per-turn dedupe state. Call it on each new user
// turn — typically when the Event handler sees role=user — and on the
// idle→busy state transition so a state-machine-driven turn still
// re-narrates its tools.
func (n *Narrator) StartTurn() {
	n.mu.Lock()
	n.turnSeen = make(map[NarrationCategory]bool)
	n.mu.Unlock()
}

// Say announces a category, deduped within the current turn. Optional
// args feed fmt.Sprintf for templates that include placeholders
// (currently only NarrateToolGeneric uses %s). Empty narration text
// or a nil speaker make this a no-op so callers don't have to guard.
func (n *Narrator) Say(cat NarrationCategory, args ...any) {
	n.mu.Lock()
	if n.turnSeen[cat] {
		n.mu.Unlock()
		return
	}
	n.turnSeen[cat] = true
	n.mu.Unlock()

	text := narrationText(n.lang, cat, args...)
	if text == "" || n.speak == nil {
		return
	}
	log.Printf("[narrator] %s → %q", cat, text)
	n.speak(text)
}

// Warmup runs every narration phrase through a prerender callback so
// the TTS cache is populated before the first real announcement.
// `prerender` should call LeLamp /voice/speak with `prerender: true`,
// which synthesizes + caches without playing. With the cache warm,
// the very first time we say "Claude is searching the web" plays from
// disk instead of waiting on the TTS provider.
func (n *Narrator) Warmup(prerender func(text string)) {
	if prerender == nil {
		return
	}
	for cat := range narrationStrings[n.lang] {
		text := narrationText(n.lang, cat)
		if text == "" {
			continue
		}
		prerender(text)
	}
}

// SayTool narrates a tool invocation. The tool name is mapped to a
// dedicated category when we have a localized phrase for it, otherwise
// it falls back to NarrateToolGeneric ("Claude is running a tool"),
// which intentionally drops the raw name — Claude Code tool names
// don't sound like words through TTS.
func (n *Narrator) SayTool(name string) {
	n.Say(toolToCategory(name))
}
