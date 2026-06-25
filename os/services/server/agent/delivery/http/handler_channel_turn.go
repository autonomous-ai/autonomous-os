package http

import (
	"fmt"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/lib/flow"
)

// channelTurnRequest is the payload the Hermes os-server-observer hook POSTs for
// every gateway turn. See internal/hermes/hooks/os-server-observer/handler.py.
type channelTurnRequest struct {
	Event   string `json:"event"` // "agent:start" | "agent:end"
	Context struct {
		Platform  string `json:"platform"` // telegram | slack | discord | api_server | cli | …
		UserID    string `json:"user_id"`
		ChatID    string `json:"chat_id"`
		ThreadID  string `json:"thread_id"`
		ChatType  string `json:"chat_type"` // dm | group | forum
		SessionID string `json:"session_id"`
		Message   string `json:"message"`  // inbound user text (agent:start); gateway truncates to 500
		Response  string `json:"response"` // assistant reply (agent:end); gateway truncates to 500
	} `json:"context"`
}

// channelHookSkipPlatforms are turns os-server already records itself: its own
// /v1/responses calls reach the gateway as the api_server platform and the device
// terminal as cli, both of which sendChat already logs to flow. Emitting here too
// would double them. Everything else is a real messaging channel we want shown.
// Matched case-insensitively with separators stripped (skipPlatform), so
// "API_SERVER" / "api-server" / "apiserver" all hit regardless of the gateway's
// exact enum spelling.
var channelHookSkipPlatforms = map[string]bool{
	"apiserver": true,
	"api":       true,
	"cli":       true,
	"terminal":  true,
}

// skipPlatform reports whether a turn from this platform should NOT be emitted as
// a channel turn (it's a device-originated turn sendChat already logged).
func skipPlatform(platform string) bool {
	norm := strings.NewReplacer("_", "", "-", "", " ", "").Replace(strings.ToLower(platform))
	return norm == "" || channelHookSkipPlatforms[norm]
}

const channelTurnTTL = 10 * time.Minute

// channelHookTracker correlates a turn's agent:start with its agent:end so both
// flow events share one run_id. Keyed by session_id — a channel session runs its
// turns sequentially, so at most one is open per session at a time.
type channelHookTracker struct {
	mu   sync.Mutex
	open map[string]openChannelTurn
	seq  atomic.Uint64
}

type openChannelTurn struct {
	runID     string
	startedAt time.Time
}

var channelHook = &channelHookTracker{open: make(map[string]openChannelTurn)}

func (t *channelHookTracker) start(platform, sessionID string) string {
	runID := fmt.Sprintf("chan-%s-%d", platform, t.seq.Add(1))
	t.mu.Lock()
	t.pruneLocked()
	t.open[sessionID] = openChannelTurn{runID: runID, startedAt: time.Now()}
	t.mu.Unlock()
	return runID
}

// end returns the run_id paired with this session's open start, or a fresh id
// when the start was missed (e.g. os-server restarted mid-turn).
func (t *channelHookTracker) end(platform, sessionID string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked()
	if o, ok := t.open[sessionID]; ok {
		delete(t.open, sessionID)
		return o.runID
	}
	return fmt.Sprintf("chan-%s-%d", platform, t.seq.Add(1))
}

func (t *channelHookTracker) pruneLocked() {
	cutoff := time.Now().Add(-channelTurnTTL)
	for k, v := range t.open {
		if v.startedAt.Before(cutoff) {
			delete(t.open, k)
		}
	}
}

// ChannelTurn receives a turn notification from the Hermes gateway observer hook
// and emits the Flow Monitor events for messaging-channel turns. This is what
// makes Telegram/Slack/Discord turns visible under Hermes — the gateway owns the
// channel I/O and never streams those turns to os-server, so without the hook
// os-server is blind to them (OpenClaw gets the equivalent via session.message).
// Channel-agnostic: the platform comes from the payload. Loopback-only; the hook
// runs on-device.
func (h *AgentHandler) ChannelTurn(c *gin.Context) {
	// Always ACK 200 — a hook error must not make the gateway retry or stall the
	// turn. Parsing/skip problems are logged, not surfaced.
	defer c.JSON(http.StatusOK, gin.H{"status": 1, "data": nil, "message": nil})

	var req channelTurnRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		return
	}
	ctx := req.Context
	if skipPlatform(ctx.Platform) {
		return
	}

	sessionID := ctx.SessionID
	if sessionID == "" {
		sessionID = ctx.Platform + ":" + ctx.ChatID
	}
	sender := ctx.UserID

	switch req.Event {
	case "agent:start":
		runID := channelHook.start(ctx.Platform, sessionID)
		flow.Log("chat_input", map[string]any{
			"run_id":  runID,
			"source":  "channel",
			"channel": ctx.Platform,
			"sender":  sender,
			"message": ctx.Message,
		}, runID)
		// Synthesise lifecycle_start so the AGENT pipeline node lights up, same
		// anchor the openclaw session.message path emits.
		flow.Log("lifecycle_start", map[string]any{
			"run_id": runID,
			"source": "channel_hook",
		}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_input",
			Summary: "[" + ctx.Platform + ":" + sender + "] " + channelTurnPreview(ctx.Message, 200),
			RunID:   runID,
			Detail:  map[string]string{"role": "user", "message": ctx.Message, "sender": sender, "channel": ctx.Platform},
		})

	case "agent:end":
		runID := channelHook.end(ctx.Platform, sessionID)
		// Close the lifecycle (RESP node → done), then carry the reply text. The
		// gateway delivered the reply to the channel, not the device speaker, so —
		// exactly like the openclaw channel path (handler_event_session_message.go)
		// — it is logged as tts_suppressed, which is the node Flow Monitor renders
		// as the turn's response in the persisted JSONL (chat_response alone only
		// lives in the monitor SSE/RAM and is lost on reload).
		flow.Log("lifecycle_end", map[string]any{
			"run_id": runID,
			"source": "channel_hook",
		}, runID)
		resp := strings.TrimSpace(ctx.Response)
		switch {
		case resp == "" || isAgentNoReply(resp):
			flow.Log("no_reply", map[string]any{"run_id": runID}, runID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: "[no reply]",
				RunID:   runID,
				State:   "final",
				Detail:  map[string]string{"role": "assistant", "message": "[no reply]", "channel": ctx.Platform},
			})
		default:
			flow.Log("tts_suppressed", map[string]any{
				"run_id": runID,
				"reason": "channel_run",
				"text":   resp,
			}, runID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: channelTurnPreview(resp, 200),
				RunID:   runID,
				State:   "final",
				Detail:  map[string]string{"role": "assistant", "message": resp, "channel": ctx.Platform},
			})
		}
	}
}

func channelTurnPreview(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}
