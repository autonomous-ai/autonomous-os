package http

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// Device-observed 2026-09-10 on lamp-a0ae: the model has no thinking channel on
// this fleet (zero `stream: "thinking"` events across a full day) so it works
// things out in the reply, and 2036 characters reached the speaker for a
// two-sentence answer. <think> gives that reasoning somewhere to go.

const thinkTurn = "<think>There's a [voice-instruction] to check Google Drive. " +
	"Per the connectors skill I must run Discover first. The transcript says " +
	"Unknown Speaker, so I should ask their name rather than enroll.</think>\n" +
	"Google Drive isn't connected — only Gmail is. What's your name?"

func TestThinkBlockIsNotSpoken(t *testing.T) {
	got := stripThinkTag(thinkTurn)
	if want := "Google Drive isn't connected — only Gmail is. What's your name?"; got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
	for _, leak := range []string{"voice-instruction", "Per the connectors skill", "Unknown Speaker"} {
		if strings.Contains(got, leak) {
			t.Errorf("reasoning %q survived: %q", leak, got)
		}
	}
}

// A reply with no block must come through byte-identical — every ordinary turn
// takes this path.
func TestReplyWithoutThinkBlockUnchanged(t *testing.T) {
	const reply = "Đèn đã bật rồi nhé!"
	if got := stripThinkTag(reply); got != reply {
		t.Errorf("plain reply changed: %q", got)
	}
}

// FAIL OPEN. A device that says nothing is worse than one that says too much,
// and saying too much is only the behaviour we already have.
func TestMalformedThinkFailsOpen(t *testing.T) {
	for name, text := range map[string]string{
		"unclosed":   "<think>I should check the calendar first and then answer",
		"only think": "<think>Nothing worth saying here.</think>",
	} {
		if got := stripThinkTag(text); got != text {
			t.Errorf("%s: must pass through untouched, got %q", name, got)
		}
	}
}

func TestMultipleThinkBlocksStripped(t *testing.T) {
	got := stripThinkTag("<think>one</think>Sent to Darren. <think>two</think>Anything else?")
	if strings.Contains(got, "one") || strings.Contains(got, "two") {
		t.Errorf("a block survived: %q", got)
	}
	if !strings.Contains(got, "Sent to Darren.") || !strings.Contains(got, "Anything else?") {
		t.Errorf("spoken text lost: %q", got)
	}
}

// The reasoning arrives token by token. Until </think> lands, nothing may be
// streamed to the speaker — a sentence already spoken cannot be unspoken.
func TestStreamingDefersWhileThinkBlockIsOpen(t *testing.T) {
	open := []string{
		"<think>There's a [voice-instruction] to check Drive. Per the skill I must run Discover.",
		"<think>one</think> ok <think>two more sentences. And another.",
		"Hold on. <t",
		"Hold on. <think",
	}
	for _, raw := range open {
		if !hasOpenThinkTag(raw) {
			t.Errorf("must defer while the block is open: %q", raw)
		}
	}
	closed := []string{
		"<think>done thinking.</think> Drive isn't connected. What's your name?",
		"<think>a</think>x<think>b</think>y. ",
		"no block here at all. ",
		"2 < 3 and 4 < 5. ",
	}
	for _, raw := range closed {
		if hasOpenThinkTag(raw) {
			t.Errorf("must not defer, block is closed: %q", raw)
		}
	}
}

// End to end through the flush path: the first sentence streamed early must be
// the first sentence of the ANSWER, never of the reasoning.
func TestFirstSentenceStreamedIsTheAnswer(t *testing.T) {
	h := &AgentHandler{
		assistantBuf:     map[string]*strings.Builder{},
		streamedCleanLen: map[string]int{},
		config:           &config.Config{STTLanguage: "en"},
	}
	const run = "device-chat-27-1789030307377"

	h.accumulateAssistantDelta(run, "<think>There's a [voice-instruction] to check Drive. Per the skill I must run Discover. ")
	if s := h.tryFirstSentenceFlush(run); s != "" {
		t.Fatalf("reasoning was streamed to the speaker: %q", s)
	}
	h.accumulateAssistantDelta(run, "</think>Google Drive isn't connected. Link it in the app and I'll walk your folders.")

	got := h.tryFirstSentenceFlush(run)
	if got != "Google Drive isn't connected." {
		t.Errorf("first spoken sentence = %q, want the answer's first sentence", got)
	}
}
