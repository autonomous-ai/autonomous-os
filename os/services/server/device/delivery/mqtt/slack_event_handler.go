package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/device"
)

const (
	// slackEventForwardTimeout caps the localhost POST so a slow/wedged gateway
	// doesn't keep the MQTT handler goroutine alive forever. Slack's signature
	// validity window is 5 min; we err well under that to surface failures fast.
	slackEventForwardTimeout = 10 * time.Second

	// slackEventDedupTTL bounds the in-memory event_id LRU. Slack retries
	// identical events up to ~5 min on non-2xx; this matches.
	slackEventDedupTTL = 5 * time.Minute

	// slackEventForwardURL is the OpenClaw gateway HTTP-mode webhook the
	// slack HTTP-mode config writes (webhookPath=/slack/events, gateway port
	// defaults to 18789).
	slackEventForwardURL = "http://127.0.0.1:18789/slack/events"

	// maxSlackErrorLength truncates the gateway error body before it rides back
	// over MQTT so a verbose 4xx can't bloat the fd_channel payload.
	maxSlackErrorLength = 500
)

// slackEventPayload is the verbatim forward shape published by the
// bff-campaign-service proxy. See domain.CommandSlackEvent docstring for wire
// format; this struct is purely for unmarshal.
type slackEventPayload struct {
	Cmd     string            `json:"cmd"`
	EventID string            `json:"event_id"`
	Body    string            `json:"body"`
	Headers map[string]string `json:"headers"`
}

// slackEventDedup is a tiny LRU keyed on Slack's event_id. We don't need a
// full LRU library: events are short-lived and the working set is small.
var slackEventDedup = struct {
	sync.Mutex
	seen map[string]time.Time
}{seen: make(map[string]time.Time)}

// rememberOrSkip returns true if the event was already seen within the TTL
// window (caller should skip), false on first sight (caller should process).
// Also opportunistically prunes expired entries on each call — cheap because
// the map is bounded by ~Slack QPS × 5 min.
func rememberOrSkip(eventID string) bool {
	if eventID == "" {
		return false // can't dedup without an ID; forward and let OpenClaw decide
	}
	slackEventDedup.Lock()
	defer slackEventDedup.Unlock()
	now := time.Now()
	if ts, ok := slackEventDedup.seen[eventID]; ok && now.Sub(ts) < slackEventDedupTTL {
		return true
	}
	slackEventDedup.seen[eventID] = now
	if len(slackEventDedup.seen) > 1024 {
		for k, v := range slackEventDedup.seen {
			if now.Sub(v) > slackEventDedupTTL {
				delete(slackEventDedup.seen, k)
			}
		}
	}
	return false
}

// publishSlackResult mirrors publishAddChannelResult's shape so the fd_channel
// response vocabulary stays consistent. cmdType is the originating command
// (domain.CommandSlackEvent or domain.CommandSlackCommand) so the ack `type` and
// info metadata distinguish a forwarded event from a forwarded slash command.
func (h *DeviceMQTTHandler) publishSlackResult(cmdType, eventID, status, errMsg string, httpStatus int) error {
	resp := map[string]any{
		"channel":     domain.ChannelSlack,
		"type":        cmdType,
		"event_id":    eventID,
		"status":      status,
		"error":       errMsg,
		"http_status": httpStatus,
		// MQTTInfoResponse embeds device/version metadata used by every
		// fa_channel→fd_channel ack; reuse it so observability stays uniform.
		"info": domain.NewMQTTInfoResponse(h.config, cmdType, device.GetDeviceMac()),
	}
	return h.publish(resp)
}

// handleSlackEvent forwards a proxy-relayed Slack Events API delivery to the
// local OpenClaw gateway. The body + signature headers are passed through
// unchanged so OpenClaw can re-verify against the same signing secret (no
// re-signing).
//
// Dedup is best-effort (per-process in-memory LRU). On reboot or a 2nd device
// owning the same workspace, OpenClaw is the second line of defence — Slack's
// event_id is included in the body and the gateway's own dedup applies.
func (h *DeviceMQTTHandler) handleSlackEvent(cmd domain.MQTTMessage) error {
	return h.forwardSlackHTTP(cmd, domain.CommandSlackEvent)
}

// handleSlackCommand forwards a proxy-relayed Slack slash command to the local
// OpenClaw gateway. Slash commands ride the SAME gateway webhook as events —
// OpenClaw's single HTTP endpoint routes by body shape (urlencoded `command=`
// vs JSON `type`) — so the wire handling is identical to handleSlackEvent. The
// proxy tags the envelope headers with Content-Type:
// application/x-www-form-urlencoded (forwarded verbatim); OpenClaw verifies the
// signature, runs the command, and replies to the user via the command's
// response_url. The cmd label differs only for dedup/observability (the proxy
// puts Slack's trigger_id in the event_id slot, since commands have no event_id).
func (h *DeviceMQTTHandler) handleSlackCommand(cmd domain.MQTTMessage) error {
	return h.forwardSlackHTTP(cmd, domain.CommandSlackCommand)
}

// forwardSlackHTTP is the shared Slack forwarder behind handleSlackEvent and
// handleSlackCommand. It POSTs the verbatim body + signature headers to the
// local OpenClaw gateway webhook; cmdType only selects the dedup/observability
// label and the fd_channel ack `type`, because OpenClaw's single HTTP endpoint
// distinguishes events from slash commands itself.
func (h *DeviceMQTTHandler) forwardSlackHTTP(cmd domain.MQTTMessage, cmdType string) error {
	var p slackEventPayload
	if err := json.Unmarshal(cmd.Raw(), &p); err != nil {
		slog.Error("slack forward: invalid payload", "component", "mqtt", "cmd", cmdType, "error", err)
		return h.publishSlackResult(cmdType, "", "failure", "invalid JSON payload", 0)
	}
	if p.Body == "" {
		slog.Error("slack forward: empty body", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID)
		return h.publishSlackResult(cmdType, p.EventID, "failure", "empty body", 0)
	}
	if rememberOrSkip(p.EventID) {
		slog.Debug("slack forward: dedup skip", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID)
		// Report skipped distinctly so the proxy can measure retry-collapse rate.
		return h.publishSlackResult(cmdType, p.EventID, "skipped_duplicate", "", 0)
	}

	// Runtime branch: SlackBridge is the generic mechanism for a runtime whose native
	// Slack support is Socket Mode only (today: hermes) and which therefore has no
	// local HTTP webhook to receive events. For such a runtime os-server IS the
	// HTTP-mode Slack frontend: it parses the event, drives a turn, and posts the
	// reply via chat.postMessage. The openclaw path below (local webhook POST) is for
	// runtimes that serve the Slack HTTP webhook themselves.
	if sb, ok := h.agentGateway.(domain.SlackBridge); ok {
		challenge, handled, err := sb.HandleInboundSlack(domain.SlackInbound{Body: p.Body})
		if err != nil {
			slog.Error("slack bridge: handle failed", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "error", err)
			return h.publishSlackResult(cmdType, p.EventID, "failure", err.Error(), 0)
		}
		if challenge != "" {
			// url_verification normally terminates at the public proxy (it owns the
			// Slack Request URL), so this is defensive; ack success either way.
			slog.Info("slack bridge: url_verification handled", "component", "mqtt", "event_id", p.EventID)
		}
		slog.Debug("slack bridge: handled", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "started_turn", handled)
		return h.publishSlackResult(cmdType, p.EventID, "success", "", 200)
	}

	ctx, cancel := context.WithTimeout(context.Background(), slackEventForwardTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, slackEventForwardURL, bytes.NewBufferString(p.Body))
	if err != nil {
		return h.publishSlackResult(cmdType, p.EventID, "failure", fmt.Sprintf("build request: %v", err), 0)
	}
	for k, v := range p.Headers {
		// Forward verbatim. Critically includes X-Slack-Signature and
		// X-Slack-Request-Timestamp so OpenClaw's HTTP-mode signature check can
		// validate against the shared signing secret, plus Content-Type
		// (application/json for events, x-www-form-urlencoded for commands) so
		// the gateway parses the body in the right shape.
		req.Header.Set(k, v)
	}
	if req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		slog.Error("slack forward: forward failed", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "error", err)
		return h.publishSlackResult(cmdType, p.EventID, "failure", fmt.Sprintf("forward: %v", err), 0)
	}
	defer resp.Body.Close()
	// Drain so the connection can be reused; the body is small and OpenClaw
	// returns either 200 OK or a 4xx with an error JSON.
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		slog.Debug("slack forward: forwarded ok", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "status", resp.StatusCode)
		return h.publishSlackResult(cmdType, p.EventID, "success", "", resp.StatusCode)
	}

	// Non-2xx — surface the gateway's error message so the proxy's retry
	// decision has signal. Truncate to keep MQTT payload small.
	errMsg := string(respBody)
	if len(errMsg) > maxSlackErrorLength {
		errMsg = errMsg[:maxSlackErrorLength]
	}
	slog.Warn("slack forward: gateway rejected", "component", "mqtt", "cmd", cmdType, "event_id", p.EventID, "status", resp.StatusCode, "body", errMsg)
	return h.publishSlackResult(cmdType, p.EventID, "failure", errMsg, resp.StatusCode)
}
