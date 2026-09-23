package jev

import (
	"regexp"
	"strings"
)

// Descriptions reflect the existing HAL behavior: dim scales the current color, and
// volume actions set levels, not deltas. Parameters use finite local allowlists.
var jevActionDescriptions = map[string]string{
	"led_on":           "User intent: switch this device's light on, e.g. 'Please switch this light on'. Use the standard on preset when no color or level is specified. Effect: warm-white RGB [255,220,180], possibly a happy expression; the user need not request these implementation details. Defer explicit colors or brightness levels.",
	"led_off":          "User intent: switch this device's light off now, e.g. 'Switch off this lamp'. Effect: light off. The request must concern this device, not another room or appliance.",
	"dim":              "Reduce this device's current light brightness by half, preserving its RGB proportions with integer rounding. Accept generic dimming, repeated requests, keeping the current color, and current complaints about excessive brightness, harshness or glare. Examples: 'This lamp is too bright', 'Still too bright', 'Make the light softer'. Uses the current effect base color or brightest pixel and sets a solid color, stopping effects and scenes. Defer explicit percentages, numeric steps, preserving an animation or pattern, and glare from the sun/screen/another light.",
	"volume_up":        "Increase this device's current speaker volume by one step of 10 percent of its safe range, clamped to its configured safe maximum. Accept generic louder requests and complaints that this device is too quiet. Defer explicit numeric levels, maximum/minimum requests, other devices, or multiple actions.",
	"volume_down":      "Reduce this device's current speaker volume by half. Accept generic quieter requests, repeated complaints, and complaints that its speech or speaker is too loud, including informal grammar: 'speak too loud', 'lamp speak too loud', 'You are too loud'. Defer explicit numeric levels, muting completely, other people/TVs/external noise, and multiple actions.",
	"led_color":        "Set this device's light to one supported named solid color, stopping its LED effect first. A single color or declared alias is required. Both violet and purple are fully supported synonyms for purple; warm white is fully supported as warm. Users need not use the canonical option spelling. Defer unspecified or unsupported colors, RGB codes, mixed colors, brightness levels, and other lights. Any named room such as bedroom denotes another lamp and must defer.",
	"scene_off":        "Exit this device's active lighting scene or mode via /scene/off. Accept disabling a named lighting scene, such as 'Leave focus mode'. This does not power down the device or mean switch on that scene.",
	"scene_reading":    "Activate this device's reading lighting scene via /scene with scene reading. Accept a present desire to read a book or get lighting for reading, including 'need focus to read book' and 'I want to read a book'. A specific reading activity takes precedence over a generic mention of focus/concentration unless focus mode is explicitly requested. This adjusts lighting, not reading content aloud or recommending books.",
	"scene_focus":      "Activate this device's focus lighting scene via /scene with scene focus. Accept a present desire for concentration, work or study lighting, such as 'I need to concentrate on work'. Explicit focus mode takes precedence; a specific book-reading activity without explicit focus mode selects scene_reading instead. This does not control another device's focus mode or provide productivity advice.",
	"scene_relax":      "Activate this device's relax lighting scene via /scene with scene relax. Example: 'Switch to relaxing lighting'. This does not start music.",
	"scene_movie":      "Activate this device's movie lighting scene via /scene with scene movie. Example: 'Set the lamp to movie mode'. This does not play a movie.",
	"scene_night":      "Activate this device's night lighting scene via /scene with scene night, possibly adding a sleepy expression. Example: 'Set this lamp to night mode'. This does not power down the device or set an alarm.",
	"scene_energize":   "Activate this device's energize lighting scene via /scene with scene energize. Accept the energize or maximum-brightness preset. Defer numeric brightness levels or relative increments.",
	"mute_speaker":     "Mute this device's speaker via /speaker/mute. Example: 'Mute your speaker'. Muting is distinct from interrupting the current spoken reply or stopping music playback.",
	"unmute_speaker":   "Unmute this device's speaker via /speaker/unmute. Example: 'Unmute your speaker'. This does not start music or set a specific volume.",
	"music_stop":       "Stop music or audio playback on this device via /audio/stop. Example: 'Stop the music you are playing'. This does not mute the speaker or interrupt only the spoken reply.",
	"stop_talking":     "Interrupt this device's current spoken reply via /tts/stop without a spoken acknowledgment. Example: 'Stop speaking now'. This does not mute the speaker or stop music playback.",
	"what_time":        "Tell the current local time on this device using its clock. Example: 'Could you tell me the time right now?' Defer other timezones, dates, elapsed durations, schedules, alarms, and questions about when an event occurs.",
	"servo_track_stop": "Stop this device's camera/servo tracking via /servo/track/stop. Example: 'Stop following me with your camera'. This does not disable online tracking, location sharing, or another device.",
	"servo_track":      "Make this device's camera/servo follow one explicitly identified supported target via /servo/track. Examples: 'Follow my face with your camera', 'Track the mug' (cup), 'Follow me' (person). Me/myself are explicit person targets, not ambiguous. Phone is a fully supported synonym for cell phone; ball means sports ball, and stuffed animal means teddy bear. These aliases satisfy the required target without further context. Defer missing or ambiguous targets, unsupported objects, multiple targets, online tracking, and requests for another camera. A security camera or a camera in a named room is external, never this robot.",
}

// Candidates returns a fresh allowlist; the caller filters it against the
// actual executable rules and positively declared hardware capabilities.
func Candidates() []Candidate {
	var candidates []Candidate
	for _, id := range []string{
		"led_on", "led_off", "dim", "volume_up", "volume_down", "led_color",
		"scene_off", "scene_reading", "scene_focus", "scene_relax", "scene_movie", "scene_night", "scene_energize",
		"mute_speaker", "unmute_speaker", "music_stop", "stop_talking", "what_time", "servo_track_stop", "servo_track",
	} {
		candidate := Candidate{ID: id, Description: jevActionDescriptions[id]}
		switch id {
		case "led_color":
			candidate.Parameters = map[string]Parameter{
				"color": {Description: "Required canonical solid color for this device's light. Violet means purple; warm white means warm. These are exact supported aliases.", Options: []string{
					"yellow", "red", "green", "blue", "cyan", "purple", "orange", "pink", "white", "warm",
				}},
			}
		case "servo_track":
			candidate.Parameters = map[string]Parameter{
				"target": {Description: "Required canonical label of one explicit target to follow with this device's camera. Me/myself means person, phone means cell phone, mug means cup, ball means sports ball, and stuffed animal means teddy bear. These are exact supported aliases.", Options: []string{
					"face", "hand", "person", "dog", "cat", "bird", "cup", "bottle", "cell phone", "book", "remote",
					"laptop", "keyboard", "mouse", "teddy bear", "sports ball", "backpack", "chair", "clock", "scissors", "banana", "apple", "orange",
				}},
			}
		}
		candidates = append(candidates, candidate)
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
