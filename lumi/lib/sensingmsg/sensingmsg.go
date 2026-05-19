// Package sensingmsg builds the LLM-bound message text for a sensing event.
// Used by both the direct sensing handler (agent idle path) and the queue
// drain (replay-after-busy path) so both share identical formatting,
// prefix/precedence rules, and pre-fetched context blocks.
package sensingmsg

import (
	"strings"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/lib/i18n"
	"go-lamp.autonomous.ai/lib/skillcontext"
)

// Build returns the message that should be forwarded to the agent for a
// sensing event. Precedence: voice_command > voice > web_chat > guard >
// passive sensing.
//
//   - currentUser: resolved attribution string. Pass the request payload's
//     CurrentUser, falling back to mood.CurrentUser(); "" is treated as
//     "unknown" for context tags. BuildPresenceContext skips "unknown" so
//     strangers don't get a "welcome back" framing.
//   - guardTag: prebuilt "[sensing:<type>][guard-active][guard-instruction:…]"
//     wrapper when guard mode is active. Pass "" otherwise (and always "" on
//     the drain path — guard state isn't preserved across the queue).
//
// pose.ergo_risk used to be its own event type; it is now folded into
// motion.activity via [posture_summary] / [computer_streak_min] blocks added
// by LeLamp's MotionPerception. The wellbeing skill reads those blocks.
func Build(eventType, message, currentUser, guardTag string) string {
	switch eventType {
	case "voice_command":
		// Wake word confirmed. `[user]` lifts to top priority in batched turns.
		return domain.AppendEnrollNudge("[user] " + message)
	case "voice":
		// Ambient speech — no wake word. `[user]` for batched-turn priority,
		// `[ambient]` for voice/SKILL.md's overheard-audio mute guard.
		return domain.AppendEnrollNudge("[user] [ambient] " + message)
	case "web_chat":
		// Typed text from the monitor. Slash commands (`/status`, `/think`, …)
		// bypass `[user]` so the agent's command router still sees the literal
		// leading slash.
		if strings.HasPrefix(message, "/") {
			return message
		}
		return "[user] " + message
	}

	if guardTag != "" {
		return guardTag + " " + message
	}

	// Passive sensing. Domain-specific prefixes route SOUL.md to the
	// dedicated skill instead of pulling in sensing/SKILL.md wholesale.
	var msg string
	switch eventType {
	case "motion.activity":
		msg = "[activity] " + message
	case "emotion.detected":
		msg = "[emotion] " + message
	case "speech_emotion.detected":
		msg = "[speech_emotion] " + message
	default:
		msg = "[sensing:" + eventType + "] " + message
	}

	if currentUser == "" {
		currentUser = "unknown"
	}

	switch eventType {
	case "presence.enter":
		// Pre-fetch "time since last seen" so sensing/SKILL.md can swap to a
		// "return after long absence" greeting without a tool turn.
		// BuildPresenceContext returns "" for unknown.
		msg += skillcontext.BuildPresenceContext(currentUser)
	case "presence.leave", "presence.away":
		msg += "\n[No crons to cancel. NO_REPLY unless worth saying.]"
	case "touch.head_pat":
		// LeLamp already played a random pet-response phrase locally; agent
		// just records the moment for memory continuity.
		msg += "\n[NO_REPLY unless worth saying — phrase already spoken locally.]"
	case "motion.activity":
		// Insert current_user right after the activity prefix line so the
		// agent sees attribution before consuming the activity payload
		// (snapshot path, computer_streak_min, posture_summary). The
		// remaining context blocks still trail at the end where they
		// don't clutter the priority section.
		parts := strings.SplitN(msg, "\n", 2)
		head := parts[0]
		tail := ""
		if len(parts) > 1 {
			tail = "\n" + parts[1]
		}
		msg = head + "\n[context: current_user=" + currentUser + "]" + tail
		msg += skillcontext.BuildUserContext(currentUser)
		// Pre-fetch wellbeing/SKILL.md reads (history + patterns + days) so
		// the skill doesn't burn a tool turn on plan-reads.
		msg += skillcontext.BuildWellbeingContext(currentUser)
	case "emotion.detected", "speech_emotion.detected":
		msg += "\n[context: current_user=" + currentUser + "]"
		msg += skillcontext.BuildUserContext(currentUser)
		// Same context serves face FER (emotion.detected) and voice
		// emotion2vec (speech_emotion.detected); the prefix tells the skill
		// which source to log on the mood signal row.
		msg += skillcontext.BuildEmotionContext(skillcontext.ExtractDetectedEmotion(message), currentUser)
	}

	// Inject device locale once per passive-sensing turn — sensor events
	// carry no user text, so SOUL.md would otherwise default to English.
	msg += i18n.LangContextTag()
	return msg
}
