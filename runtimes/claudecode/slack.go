package claudecode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// Device-owned Slack inbound (HTTP mode) for the Claude Code runtime, modeled
// on runtimes/codex/slack.go (itself a mirror of runtimes/hermes/slack.go).
//
// Claude Code has no slack channel plugin ("Claude in Slack" is a separate
// cloud feature), so this path is device-owned: the public bff-campaign-service
// proxy receives Slack Events API deliveries and fans them out over MQTT to the
// device's slack_event handler (server/device/delivery/mqtt/
// slack_event_handler.go), which dedups by event_id and type-asserts the active
// gateway to domain.SlackBridge. ClaudeCodeService implements that bridge, so
// while claudecode is the active runtime a forwarded Slack message becomes a
// regular claudecode turn: the run is tracked in slackRuns (reply routing) and
// marked silent (no TTS), and emitFinal posts the reply back to the originating
// channel/thread via chat.postMessage (translator.go).
//
// Intentional differences from hermes: NO progressive streaming
// (chat.startStream/appendStream — the final answer arrives whole on the
// `result` event, so the reply is posted once) and NO assistant "…is typing"
// status (that affordance belongs to the streaming UX). The eyes ack reaction
// is mirrored: added when the turn is injected, cleared when the reply (or
// error) lands.

// Compile-time check: *ClaudeCodeService implements domain.SlackBridge, which is
// what the slack_event MQTT handler type-asserts to route inbound events here.
var _ domain.SlackBridge = (*ClaudeCodeService)(nil)

const (
	// slackBusyPoll is how often an accepted Slack message rechecks IsBusy()
	// before injecting its turn (mirrors codex's slackBusyPoll).
	slackBusyPoll = 500 * time.Millisecond

	// slackBusyWaitMax caps the busy wait so a wedged agent cannot pin the
	// injection goroutine forever; past it the message is dropped (Slack will
	// not re-deliver — the MQTT handler already acked the event).
	slackBusyWaitMax = 2 * time.Minute
)

// slackRun records where an inbound Slack turn came from so emitFinal can post
// the reply in the same channel/thread and clear the ack reaction.
type slackRun struct {
	channel   string
	threadTS  string
	messageTS string // the user message ts — used to add/remove the eyes reaction
}

// slackEnvelope is the outer Slack Events API shape the bff proxy forwards
// verbatim. Only the fields we branch on are modeled; the rest is ignored.
type slackEnvelope struct {
	Type      string     `json:"type"`      // "url_verification" | "event_callback"
	Challenge string     `json:"challenge"` // url_verification only
	Event     slackEvent `json:"event"`     // event_callback only
}

type slackEvent struct {
	Type     string `json:"type"`      // "message" | "app_mention" | ...
	Subtype  string `json:"subtype"`   // "message_changed", "bot_message", ... (skip when set)
	Text     string `json:"text"`      //
	User     string `json:"user"`      // Slack user id (empty for bot/system events)
	BotID    string `json:"bot_id"`    // set on bot messages — loop guard
	Channel  string `json:"channel"`   //
	TS       string `json:"ts"`        //
	ThreadTS string `json:"thread_ts"` //
}

// slackMentionRE strips a leading bot mention (<@U123ABC>) from app_mention text.
var slackMentionRE = regexp.MustCompile(`^\s*<@[A-Z0-9]+>\s*`)

// parsedSlackMsg is a real user message extracted from a Slack event.
type parsedSlackMsg struct {
	text      string
	user      string
	channel   string
	threadTS  string
	messageTS string
}

// parseSlackInbound is the pure (no-I/O) decode of a forwarded Slack event,
// identical in semantics to codex/hermes' parseSlackInbound:
//   - challenge != "" — a url_verification handshake.
//   - msg != nil       — a real user message to turn into an agent turn.
//   - both empty/nil   — intentionally ignored (bot loop, non-message, disallowed user).
//
// allowedUser, when non-empty, restricts which Slack user may drive turns.
func parseSlackInbound(body, allowedUser string) (challenge string, msg *parsedSlackMsg, err error) {
	var env slackEnvelope
	if uerr := json.Unmarshal([]byte(body), &env); uerr != nil {
		return "", nil, fmt.Errorf("parse slack event: %w", uerr)
	}
	if env.Type == "url_verification" {
		return env.Challenge, nil, nil
	}
	if env.Type != "event_callback" {
		return "", nil, nil
	}
	ev := env.Event
	if ev.Type != "message" && ev.Type != "app_mention" {
		return "", nil, nil // not a user message
	}
	// Loop guard + non-user events: bot's own messages, edits/joins/etc.
	if ev.BotID != "" || ev.Subtype != "" || ev.User == "" {
		return "", nil, nil
	}
	// Allowed-user gate: empty allowedUser = open (the workspace/app already scopes access).
	if allowedUser != "" && ev.User != allowedUser {
		return "", nil, nil
	}
	text := strings.TrimSpace(slackMentionRE.ReplaceAllString(ev.Text, ""))
	if text == "" {
		return "", nil, nil
	}
	return "", &parsedSlackMsg{text: text, user: ev.User, channel: ev.Channel, threadTS: ev.ThreadTS, messageTS: ev.TS}, nil
}

// HandleInboundSlack implements domain.SlackBridge. It parses a forwarded
// Slack event, answers a url_verification handshake, and otherwise injects the
// message as a claudecode turn. Injection happens in a goroutine (the agent may
// be busy and claudecode correlates turns by a single in-flight runID, so the
// turn waits for idle) — handled == true therefore means "accepted for
// injection", and the MQTT handler acks immediately.
func (s *ClaudeCodeService) HandleInboundSlack(in domain.SlackInbound) (string, bool, error) {
	challenge, msg, err := parseSlackInbound(in.Body, s.config.SlackUserID)
	if err != nil {
		return "", false, err
	}
	if challenge != "" {
		slog.Info("slack: url_verification challenge", "component", "claudecode")
		return challenge, false, nil
	}
	if msg == nil {
		return "", false, nil // intentionally ignored
	}

	// Reply in the existing thread, else thread under the user's message
	// (mirrors codex/hermes' thread handling).
	threadTS := msg.threadTS
	if threadTS == "" {
		threadTS = msg.messageTS
	}
	// Prefix sender metadata so the agent knows who is talking and on which
	// channel — same shape as codex's slack path.
	turnText := fmt.Sprintf("[slack] Message from <@%s> [channel:%s]:\n%s", msg.user, msg.channel, msg.text)
	go s.injectSlackTurn(turnText, slackRun{channel: msg.channel, threadTS: threadTS, messageTS: msg.messageTS})
	slog.Info("slack: inbound accepted for claudecode turn", "component", "claudecode", "channel", msg.channel)
	return "", true, nil
}

// injectSlackTurn waits for the agent to go idle, then sends the message as a
// regular chat turn with flow source "slack" (chat_input / chat_send flow
// events fire as usual, so Flow Monitor shows the origin). The runID is
// tracked in slackRuns so emitFinal posts the reply back to the originating
// channel/thread, and marked silent so the reply is not spoken over TTS.
func (s *ClaudeCodeService) injectSlackTurn(text string, origin slackRun) {
	deadline := time.Now().Add(slackBusyWaitMax)
	for s.IsBusy() {
		if time.Now().After(deadline) {
			slog.Warn("slack turn dropped (agent busy past cap)",
				"component", "claudecode", "channel", origin.channel, "cap", slackBusyWaitMax)
			return
		}
		time.Sleep(slackBusyPoll)
	}
	reqID, runID := s.NextChatRunID()
	s.markSlackRun(runID, origin)
	s.MarkSilentRun(runID) // reply goes back to the Slack thread, not TTS
	send := s.slackSendTurn
	if send == nil {
		send = func(text, reqID, runID string) error {
			_, err := s.sendChat(text, nil, reqID, runID, "slack")
			return err
		}
	}
	if err := send(text, reqID, runID); err != nil {
		// Un-mark so the trackers don't leak. The message is dropped (the MQTT
		// handler already acked, so Slack will not retry); the user sees no
		// reply and can resend.
		s.consumeSlackRun(runID)
		s.ConsumeSilentRun(runID)
		slog.Error("slack turn injection failed",
			"component", "claudecode", "runID", runID, "channel", origin.channel, "error", err)
		return
	}
	// Acknowledge receipt with an eyes reaction (best-effort, mirrors codex /
	// hermes); finishSlackTurn clears it when the reply lands.
	if origin.messageTS != "" {
		if err := s.setSlackReaction(true, origin.channel, origin.messageTS, slackAckReaction); err != nil {
			slog.Debug("slack: ack reaction failed (non-fatal)", "component", "claudecode", "err", err)
		}
	}
}

// IsSlackOriginRun implements domain.SlackBridge — non-consuming peek so the
// SSE handler's mid-turn gates can see a Slack turn. TTS suppression itself
// rides the silent-run marker (set at injection).
func (s *ClaudeCodeService) IsSlackOriginRun(runID string) bool {
	return s.hasSlackRun(runID)
}

// StreamSlackDelta implements domain.SlackBridge — intentionally a no-op:
// Claude Code's final answer arrives whole on the `result` event (the bridge
// does not enable partial deltas), so there is nothing to stream
// progressively; the full reply is posted once by emitFinal.
func (s *ClaudeCodeService) StreamSlackDelta(string, string) {}

// DeliverSlackReply implements domain.SlackBridge. In practice emitFinal /
// handleError consume the slackRuns entry synchronously BEFORE dispatching the
// lifecycle events the shared agent handler reacts to, so this is a safety net
// (and satisfies the bridge contract): it consumes the origin if still
// present, clears the ack reaction, and posts text when non-empty. No-op for
// non-Slack runs.
func (s *ClaudeCodeService) DeliverSlackReply(runID, text string) error {
	o, ok := s.consumeSlackRun(runID)
	if !ok {
		return nil // not a Slack-origin run (or already finalized by emitFinal)
	}
	s.finishSlackTurn(o, text)
	return nil
}

// finishSlackTurn finalizes a Slack-origin turn: clears the eyes ack reaction
// (best-effort) and posts the reply to the originating channel/thread when
// non-empty. Called from emitFinal (reply), handleError (reply == "": cleanup
// only) and DeliverSlackReply (safety net).
func (s *ClaudeCodeService) finishSlackTurn(o slackRun, reply string) {
	if o.messageTS != "" {
		if err := s.setSlackReaction(false, o.channel, o.messageTS, slackAckReaction); err != nil {
			slog.Debug("slack: clear ack reaction failed (non-fatal)", "component", "claudecode", "err", err)
		}
	}
	if reply == "" {
		return
	}
	if err := s.postSlackMessage(o.channel, o.threadTS, reply); err != nil {
		slog.Error("slack reply send failed", "component", "claudecode", "channel", o.channel, "error", err)
		return
	}
	slog.Info("slack reply posted", "component", "claudecode", "channel", o.channel, "threaded", o.threadTS != "")
}
