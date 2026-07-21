package http

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// poseBucketRoot is the on-disk base where hal writes pose buckets.
// Matches hal/config.py:SNAPSHOT_TMP_DIR + "/sensing_pose/buckets/".
// hal and os-server share the same Pi so this is the same FS location for
// both processes.
const poseBucketRoot = "/tmp/hal-sensing-snapshots/sensing_pose/buckets"

// buildPoseBucketImagePaths joins a bucket id with each worst-snapshot
// filename to produce absolute paths Telegram can read. Filenames that
// would escape the bucket dir (path separators, "..") are dropped.
func buildPoseBucketImagePaths(bucketID string, filenames []string) []string {
	if bucketID == "" || len(filenames) == 0 {
		return nil
	}
	if strings.ContainsAny(bucketID, "/\\") || strings.Contains(bucketID, "..") {
		return nil
	}
	paths := make([]string, 0, len(filenames))
	for _, f := range filenames {
		if strings.ContainsAny(f, "/\\") || strings.Contains(f, "..") {
			continue
		}
		paths = append(paths, filepath.Join(poseBucketRoot, bucketID, f))
	}
	return paths
}

// HandleEvent processes incoming WebSocket events from the OpenClaw gateway.
func (h *AgentHandler) HandleEvent(ctx context.Context, evt domain.WSEvent) error {
	slog.Debug("event received", "component", "agent", "event", evt.Event)

	// OpenClaw cron events: action="started" fires immediately before the
	// agent lifecycle_start for a cron-triggered turn. Payload schema (from
	// src/cron/service/state.ts CronEvent): { jobId, action, sessionKey,
	// runAtMs, ... }. We cache sessionKey → timestamp; the next lifecycle_start
	// matching that sessionKey within cronFireWindowMs gets marked as a cron
	// fire so isChannelRun is overridden and TTS reaches the device speaker.
	if evt.Event == "cron" {
		// Diagnostic: dump raw cron payload — keep until correlation is proven
		// stable across all sessionTarget variants.
		slog.Info("cron event raw payload", "component", "agent", "payload", string(evt.Payload))
		var cronEvt struct {
			Action  string `json:"action"`
			JobID   string `json:"jobId"`
			RunAtMs int64  `json:"runAtMs"`
		}
		if err := json.Unmarshal(evt.Payload, &cronEvt); err == nil && cronEvt.Action == "started" {
			now := time.Now().UnixMilli()
			h.cronFireExpectedMu.Lock()
			// Prune stale entries before pushing — bounds queue growth.
			cutoff := now - cronFireWindowMs
			pruned := h.cronFireExpected[:0]
			for _, ts := range h.cronFireExpected {
				if ts >= cutoff {
					pruned = append(pruned, ts)
				}
			}
			h.cronFireExpected = append(pruned, now)
			h.cronFireExpectedMu.Unlock()
			slog.Info("cron started — expecting lifecycle_start", "component", "agent", "job_id", cronEvt.JobID, "run_at_ms", cronEvt.RunAtMs)
		}
	}

	switch evt.Event {
	case "agent":
		return h.handleAgentStreamEvent(evt)
	case "session.tool":
		return h.handleSessionToolEvent(evt)
	case "chat":
		return h.handleChatEvent(evt)
	case "session.message":
		return h.handleSessionMessageEvent(evt)
	default:
		// Unhandled WS events (health, heartbeat, cron, shutdown, etc.) — no-op.
	}

	return nil
}

// parseHistoryTimestamp accepts both shapes OpenClaw uses for message
// timestamps: RFC3339 strings (session store) and unix milliseconds (chat
// events / some chat.history responses). Returns the zero time when the field
// is absent or unparseable — callers treat zero as "fresh" to keep the old
// behavior on gateways that omit timestamps.
func parseHistoryTimestamp(raw json.RawMessage) time.Time {
	if len(raw) == 0 {
		return time.Time{}
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if ts, err := time.Parse(time.RFC3339Nano, s); err == nil {
			return ts
		}
		return time.Time{}
	}
	var ms int64
	if json.Unmarshal(raw, &ms) == nil && ms > 0 {
		return time.UnixMilli(ms)
	}
	return time.Time{}
}

// extractLastUserMessageFromHistory parses a chat.history payload and returns
// the most recent role:"user" message text, its senderLabel (empty if absent)
// and its timestamp (zero when missing/unparseable — older gateways). Content
// can be a plain string or an array of {type,text} blocks; both shapes are
// handled. Returns ("","",zero) if the payload is malformed or has no user
// messages. Callers use the timestamp to reject STALE messages: a fetch fired
// at lifecycle_start can race OpenClaw persisting the new message and see only
// the previous turn's input (heartbeat runs used to clone the prior turn's
// [activity] text in the Flow monitor this way).
func extractLastUserMessageFromHistory(payload json.RawMessage) (text string, senderLabel string, msgTime time.Time) {
	var hist struct {
		Messages []struct {
			Role        string          `json:"role"`
			Timestamp   json.RawMessage `json:"timestamp"`
			Content     json.RawMessage `json:"content"`
			SenderLabel string          `json:"senderLabel"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return "", "", time.Time{}
	}
	for i := len(hist.Messages) - 1; i >= 0; i-- {
		if hist.Messages[i].Role != "user" {
			continue
		}
		senderLabel = hist.Messages[i].SenderLabel
		msgTime = parseHistoryTimestamp(hist.Messages[i].Timestamp)
		var s string
		if json.Unmarshal(hist.Messages[i].Content, &s) == nil {
			return s, senderLabel, msgTime
		}
		var blocks []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if json.Unmarshal(hist.Messages[i].Content, &blocks) == nil {
			var parts []string
			for _, b := range blocks {
				if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
					parts = append(parts, b.Text)
				}
			}
			return strings.Join(parts, " "), senderLabel, msgTime
		}
		return "", senderLabel, msgTime
	}
	return "", "", time.Time{}
}
