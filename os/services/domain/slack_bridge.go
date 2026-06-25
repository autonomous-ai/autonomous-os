package domain

// SlackInbound carries a Slack Events API delivery forwarded verbatim by the
// bff-campaign-service proxy over MQTT (the same body the openclaw HTTP-mode path
// POSTs to the local gateway).
type SlackInbound struct {
	// Body is the raw Slack Events API JSON (event_callback or url_verification).
	Body string
}

// SlackBridge is implemented by runtimes that act as their own Slack frontend in
// os-server (today: hermes, which has no HTTP Slack webhook of its own — its native
// Slack support is Socket Mode only). The slack_event MQTT handler and the agent SSE
// reply path type-assert the active gateway to this interface; runtimes that don't
// implement it (openclaw, picoclaw) keep the existing behavior untouched.
type SlackBridge interface {
	// HandleInboundSlack parses a forwarded Slack event and drives an agent turn.
	//   - challenge != "" — a url_verification handshake; the caller must echo the
	//     challenge back so the proxy can answer Slack's URL check (no turn started).
	//   - handled == false — the event was intentionally ignored (bot's own message,
	//     a non-message event, a disallowed user); the caller acks success, no turn.
	//   - handled == true — a turn was started (and the message acknowledged with an
	//     eyes reaction); the reply is delivered later via DeliverSlackReply.
	HandleInboundSlack(in SlackInbound) (challenge string, handled bool, err error)

	// IsSlackOriginRun reports whether runID came from an inbound Slack event,
	// WITHOUT consuming the origin. Used by the SSE handler to suppress speaker TTS
	// (both the mid-turn first-sentence stream and the final remainder) for a Slack
	// turn — the reply belongs in Slack, not on the device speaker.
	IsSlackOriginRun(runID string) bool

	// StreamSlackDelta feeds the cleaned cumulative reply text so far for a
	// Slack-origin run. The bridge throttles + appends the new portion to the live
	// Slack stream (chat.appendStream) so the reply renders progressively under the
	// native "…is typing" indicator. cleanTextSoFar is the full sanitized text up to
	// now (HW markers stripped); the bridge diffs against what it has appended.
	StreamSlackDelta(runID string, cleanTextSoFar string)

	// DeliverSlackReply finalizes a Slack-origin turn: flushes the remaining stream
	// text and closes it (chat.stopStream, which also clears the typing indicator),
	// then clears the eyes reaction. Falls back to a plain chat.postMessage when no
	// live stream exists (e.g. startStream failed). Consumes the origin; a no-op for
	// non-Slack runs.
	DeliverSlackReply(runID string, text string) error
}
