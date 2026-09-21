package jev

import (
	"regexp"
	"strings"
)

// Only parameter-free actions are exposed. Descriptions reflect the existing
// HAL behavior: dim changes color, and volume actions set levels, not deltas.
var jevActionDescriptions = map[string]string{
	"led_on":      "Turn on this device's light with warm-white RGB [255,220,180]; may also show its happy expression. No particular color or brightness requested.",
	"led_off":     "Turn off this device's light now.",
	"dim":         "Make this device's light dim warm-white, RGB [80,60,40]. No specific brightness, color-preservation, relative step or percentage requested.",
	"volume_up":   "Set this device's speaker volume to its configured safe maximum, not a relative increment. No specific level or small step requested.",
	"volume_down": "Set this device's speaker volume to 30 percent of its configured safe maximum, not a relative decrement. No specific level or small step requested.",
}

// Candidates returns a fresh allowlist; the caller filters it against the
// actual executable rules and positively declared hardware capabilities.
func Candidates() []Candidate {
	var candidates []Candidate
	for _, id := range []string{"led_on", "led_off", "dim", "volume_up", "volume_down"} {
		candidates = append(candidates, Candidate{ID: id, Description: jevActionDescriptions[id]})
	}
	return candidates
}

const (
	instructionMarker = "[voice-instruction]"
	transcriptMarker  = "[transcript]"
)

var (
	reSnapshotTag = regexp.MustCompile(`\[snapshot:[^\]]*\]`)
	reVisionHint  = regexp.MustCompile(`\[vision-image\][^\n]*`)
)

// Preserve the authoritative voice instruction (including pronouns/negation),
// without concatenating it with a potentially contradictory STT transcript.
// Do not truncate: losing a trailing condition could change an action's meaning.
func jevText(text string) string {
	text = reSnapshotTag.ReplaceAllString(text, " ")
	text = reVisionHint.ReplaceAllString(text, " ")
	if i := strings.LastIndex(text, transcriptMarker); i >= 0 {
		if j := strings.Index(text[:i], instructionMarker); j >= 0 {
			instruction := strings.TrimSpace(text[j+len(instructionMarker) : i])
			if instruction != "" {
				return instruction
			}
		}
		text = text[i+len(transcriptMarker):]
	} else if i := strings.Index(text, instructionMarker); i >= 0 {
		text = text[i+len(instructionMarker):]
	}
	return cleanJevTranscript(text)
}

var (
	jevSpeakerPrefix = regexp.MustCompile(`^(?:Unknown Speaker|Speaker - [^:\r\n]{1,80}):\s*`)
	jevAudioSuffix   = regexp.MustCompile(`\s+\(audio (?:saved at [^()\r\n]+|is too short[^()\r\n]*)\)\s*$`)
)

// Unlike chitchat normalization, unknown brackets are user content: stripping
// "[don't]" or "[when I get home]" would silently change the requested action.
func cleanJevTranscript(text string) string {
	text = strings.TrimSpace(text)
	for {
		before := text
		for _, prefix := range []string{"[user]", "[ambient]"} {
			text = strings.TrimSpace(strings.TrimPrefix(text, prefix))
		}
		text = jevSpeakerPrefix.ReplaceAllString(text, "")
		if strings.HasPrefix(text, "[voice:") {
			if end := strings.Index(text, "]"); end >= 0 {
				text = strings.TrimSpace(text[end+1:])
			}
		}
		if text == before {
			break
		}
	}
	return strings.TrimSpace(jevAudioSuffix.ReplaceAllString(text, ""))
}
