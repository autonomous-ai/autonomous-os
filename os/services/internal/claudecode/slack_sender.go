package claudecode

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Slack Web API client for the Claude Code runtime, mirroring codex'
// slack_sender.go (itself hermes' minus the streaming methods —
// chat.startStream / appendStream / stopStream / assistant status): the reply
// is posted once.

// slackAPIBaseDefault is the production Slack Web API host. Tests override the
// service field slackAPIBase with an httptest URL.
const slackAPIBaseDefault = "https://slack.com/api"

// slackAckReaction is the emoji added to an inbound message while the turn
// runs and removed when the reply lands (mirrors codex / hermes'
// "eyes while thinking").
const slackAckReaction = "eyes"

// slackAPIResponse is the subset of a Slack Web API reply we inspect.
type slackAPIResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error"`
	TS    string `json:"ts"`
}

// slackAPI performs a JSON POST to a Slack Web API method. Slack returns HTTP
// 200 with {"ok":false,"error":...} on logical failures, so the body is always
// parsed. benignErrors are treated as success (e.g. already_reacted /
// no_reaction). The bot token is read fresh from Device config per call, so
// saving or rotating it needs no restart.
func (s *ClaudeCodeService) slackAPI(method string, payload map[string]any, benignErrors ...string) (slackAPIResponse, error) {
	var out slackAPIResponse
	if s.config.SlackBotToken == "" {
		return out, fmt.Errorf("slack bot token not configured")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return out, fmt.Errorf("marshal slack payload: %w", err)
	}
	base := s.slackAPIBase
	if base == "" {
		base = slackAPIBaseDefault
	}
	req, err := http.NewRequest(http.MethodPost, base+"/"+method, bytes.NewReader(body))
	if err != nil {
		return out, fmt.Errorf("build slack request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.config.SlackBotToken)
	req.Header.Set("Content-Type", "application/json; charset=utf-8")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return out, fmt.Errorf("slack request: %w", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if uerr := json.Unmarshal(raw, &out); uerr != nil {
		return out, fmt.Errorf("parse slack response (status %d): %w", resp.StatusCode, uerr)
	}
	if !out.OK {
		for _, b := range benignErrors {
			if out.Error == b {
				return out, nil
			}
		}
		return out, fmt.Errorf("slack %s failed: %s", method, out.Error)
	}
	return out, nil
}

// postSlackMessage posts text to a channel (threaded when threadTS != "") via
// chat.postMessage.
func (s *ClaudeCodeService) postSlackMessage(channel, threadTS, text string) error {
	payload := map[string]any{"channel": channel, "text": text}
	if threadTS != "" {
		payload["thread_ts"] = threadTS
	}
	_, err := s.slackAPI("chat.postMessage", payload)
	return err
}

// setSlackReaction adds (add=true) or removes a reaction emoji on a message.
// Benign double add/remove ("already_reacted" / "no_reaction") is treated as
// success.
func (s *ClaudeCodeService) setSlackReaction(add bool, channel, ts, name string) error {
	if channel == "" || ts == "" {
		return nil
	}
	method := "reactions.remove"
	if add {
		method = "reactions.add"
	}
	_, err := s.slackAPI(method, map[string]any{"channel": channel, "timestamp": ts, "name": name}, "already_reacted", "no_reaction")
	return err
}

// SlackSender delivers proactive (sensing/broadcast) messages to Slack,
// mirroring codex/hermes' SlackSender: it posts to the configured Slack
// user/channel (config.SlackUserID).
type SlackSender struct {
	svc *ClaudeCodeService
}

func (t *SlackSender) Name() string { return "slack" }

func (t *SlackSender) IsConfigured() bool {
	return t.svc.config.SlackBotToken != "" && t.svc.config.SlackUserID != ""
}

func (t *SlackSender) Send(msg string, _ string) error {
	// Image attachments are not supported on the Slack proactive path (the
	// reply path is text-only too). Drop the image, send the text.
	return t.svc.postSlackMessage(t.svc.config.SlackUserID, "", msg)
}
