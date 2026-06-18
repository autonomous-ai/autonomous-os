package http

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
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

// extractLastUserMessageFromHistory parses a chat.history payload and returns
// the most recent role:"user" message text plus its senderLabel (empty if
// absent). Content can be a plain string or an array of {type,text} blocks;
// both shapes are handled. Returns ("","") if the payload is malformed or has
// no user messages.
func extractLastUserMessageFromHistory(payload json.RawMessage) (text string, senderLabel string) {
	var hist struct {
		Messages []struct {
			Role        string          `json:"role"`
			Content     json.RawMessage `json:"content"`
			SenderLabel string          `json:"senderLabel"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return "", ""
	}
	for i := len(hist.Messages) - 1; i >= 0; i-- {
		if hist.Messages[i].Role != "user" {
			continue
		}
		senderLabel = hist.Messages[i].SenderLabel
		var s string
		if json.Unmarshal(hist.Messages[i].Content, &s) == nil {
			return s, senderLabel
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
			return strings.Join(parts, " "), senderLabel
		}
		return "", senderLabel
	}
	return "", ""
}
