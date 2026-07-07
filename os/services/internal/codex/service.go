// Package codex implements domain.AgentGateway against the OpenAI Codex CLI
// reached over a persistent WebSocket to a thin local bridge. See
// docs/agentic/codex.md for the protocol mapping and the runtime boundaries
// with OpenClaw / Hermes / PicoClaw.
//
// Codex has no server mode, so the codex systemd unit runs a bridge
// (presync-materialized /root/.codex/bridge.py) that spawns ONE `codex exec
// --json` subprocess per turn (resuming the persisted thread id) and exposes
// the socket at WSURL. os-server only acts as a client: it sends user turns
// as `message.send`, and translates the forwarded Codex exec JSONL events
// (thread.started / item.* / turn.completed / turn.failed) into the same
// domain.WSEvent shape that the OpenClaw handler at server/agent/delivery/
// http/handler_events.go consumes — so the downstream pipeline (HAL TTS,
// [HW:/...] markers, monitor SSE, sensing drain, Telegram fan-out) stays
// untouched.
//
// Like PicoClaw, there is no per-frame runId on the wire: the final answer
// arrives whole on `item.completed` (agent_message) + `turn.completed`, and
// turns are correlated by a single in-flight runID (the bridge serializes
// turns; codex exec handles one turn per process).
package codex

import (
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/monitor"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/server/config"
)

// Compile-time check: *CodexService implements domain.AgentGateway.
var _ domain.AgentGateway = (*CodexService)(nil)

// reSnapshotPath / rePoseBucketMarker / rePoseWorstMarker mirror the openclaw
// regexes so the drain pipeline strips the same markers before send.
var (
	reSnapshotPath     = regexp.MustCompile(`\[snapshot:\s*[^\]]+\]`)
	rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
	rePoseWorstMarker  = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)
)

// extractPoseBucketMarkers pulls (bucket_id, filenames) from a sensing message.
func extractPoseBucketMarkers(message string) (string, []string) {
	bm := rePoseBucketMarker.FindStringSubmatch(message)
	if bm == nil {
		return "", nil
	}
	bucketID := strings.TrimSpace(bm[1])
	if bucketID == "" {
		return "", nil
	}
	wm := rePoseWorstMarker.FindStringSubmatch(message)
	var worst []string
	if wm != nil {
		for _, part := range strings.Split(wm[1], ",") {
			part = strings.TrimSpace(part)
			if part != "" {
				worst = append(worst, part)
			}
		}
	}
	return bucketID, worst
}

// CodexService is the Codex backend implementation of domain.AgentGateway.
type CodexService struct {
	config     *config.Config
	monitorBus *monitor.Bus
	statusLED  *statusled.Service

	// Persistent WebSocket. wsConn is set once connected and nil'd on drop.
	wsMu           sync.Mutex
	wsConn         *websocket.Conn
	wsConnected    atomic.Bool
	wsConnectedAt  atomic.Int64 // unix seconds when the socket last became ready
	wsHasConnected atomic.Bool  // skip "reconnect" TTS on first successful connect

	// Turn lifecycle. activeTurn flips true on SendChat (write) and false on the
	// final / error frame (read). pendingRunID is the runID allocated by an
	// outbound SendChat, adopted by the first inbound frame of that turn;
	// currentRunID is the runID of the turn currently being streamed back.
	activeTurn   atomic.Bool
	busySince    atomic.Int64
	pendingRunID atomic.Value // string
	currentRunID atomic.Value // string
	reqCounter   atomic.Int64

	// Session state. sessionUUID is the Claude-assigned session_id captured from
	// any inbound frame.
	sessionUUID atomic.Value // string

	// turnMu guards the per-turn accumulation state below. The translator runs
	// on the single WS read goroutine, but clearTurn is also called from the
	// client teardown path, so the shared state stays lock-protected.
	turnMu sync.Mutex
	// assistantParts collects agent_message texts of the in-flight turn; joined
	// into the final reply at turn.completed (codex exec does not stream deltas).
	assistantParts []string
	// toolStartSeen tracks item ids whose tool.start was already emitted, so an
	// item.completed without a prior item.started still yields a start+end pair.
	toolStartSeen map[string]bool

	// mcpMu serializes config.toml [mcp_servers] read-modify-write cycles (mcp.go).
	mcpMu sync.Mutex

	// Pending sensing events buffered while busy.
	pendingEventsMu sync.Mutex
	pendingEvents   []pendingEvent

	// Run trackers (guard / broadcast / web_chat / silent / pose bucket).
	guardRunsMu sync.Mutex
	guardRuns   map[string]string

	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	// telegramRuns maps a Telegram-originated runID → originating chat id so
	// emitFinal DMs the reply back (see telegram_poll.go / translator.go).
	telegramRunsMu sync.Mutex
	telegramRuns   map[string]string

	// slackRuns maps a Slack-originated runID → its origin channel/thread so
	// emitFinal posts the reply back (see slack.go / translator.go).
	slackRunsMu sync.Mutex
	slackRuns   map[string]slackRun

	// discordRuns maps a Discord-originated runID → originating channel id so
	// emitFinal posts the reply back (see discord.go / translator.go).
	discordRunsMu sync.Mutex
	discordRuns   map[string]string

	// discordSession is the live gateway session handle (discord.go), guarded
	// so the reply sender works outside the handler goroutine.
	discordMu      sync.Mutex
	discordSession *discordgo.Session

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// Channel senders (Telegram Bot API): proactive alerts + reply DMs for
	// Telegram-originated turns. The inbound counterpart is the device-owned
	// getUpdates poll loop started from StartWS — see telegram_poll.go.
	channels []domain.ChannelSender

	// Telegram inbound test seams (telegram_poll.go). Zero values select the
	// production defaults: api.telegram.org, the /root/.codex state files, and
	// the real sendChat-backed send step.
	telegramAPIBase     string
	telegramOffsetPath  string
	telegramTargetsPath string
	telegramSendTurn    func(text, reqID, runID string) error

	// Slack inbound test seams (slack.go / slack_sender.go). Zero values select
	// the production defaults: slack.com/api and the real sendChat-backed send step.
	slackAPIBase  string
	slackSendTurn func(text, reqID, runID string) error

	// Discord inbound test seams (discord.go). Zero values select the
	// production defaults: the real sendChat-backed send step and the live
	// discordgo session's ChannelMessageSend.
	discordSendTurn    func(text, reqID, runID string) error
	discordSendMessage func(channelID, text string) error

	// ackHookEnabled mirrors OpenClaw's emotion-acknowledge hook: when the device
	// declares the `expression` capability, every visible turn flashes a "thinking"
	// face before the reply lands. Resolved once at construction from the shared
	// hook registry (skills.SupportedHooks). See emotion_ack.go.
	ackHookEnabled bool

	// Pending chat traces (idempotencyKey ↔ message text for MatchPendingByMessage).
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace

	// Recent outbound texts (echo-suppression for session.message handler).
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound
}

type recentOutbound struct {
	text string
	ts   int64
}

const recentOutboundWindowMs int64 = 30_000
const recentOutboundMaxEntries = 32

type pendingTrace struct {
	runID   string
	message string
	sentAt  time.Time
}

type poseBucketInfo struct {
	bucketID  string
	filenames []string
	markedAt  time.Time
}

// ProvideService constructs the Codex service. Wired via internal/agent/factory.go
// when config.AgentRuntime == "codex".
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *CodexService {
	s := &CodexService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		telegramRuns:   make(map[string]string),
		slackRuns:      make(map[string]slackRun),
		discordRuns:    make(map[string]string),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
		&SlackSender{svc: s},
	}
	s.ackHookEnabled = ackEmotionEnabled(cfg.DeviceTypeOrDefault())
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *CodexService) Name() string { return "Codex" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *CodexService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *CodexService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — Codex does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle. Returns 0 (unknown).
func (s *CodexService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.CodexService. Used by the
// session.message handler to skip echoes of Device-injected user messages.
func (s *CodexService) markOutboundChat(text string) {
	if text == "" {
		return
	}
	now := time.Now().UnixMilli()
	s.recentOutboundMu.Lock()
	defer s.recentOutboundMu.Unlock()
	cutoff := now - recentOutboundWindowMs
	pruned := s.recentOutboundTexts[:0]
	for _, r := range s.recentOutboundTexts {
		if r.ts >= cutoff {
			pruned = append(pruned, r)
		}
	}
	pruned = append(pruned, recentOutbound{text: text, ts: now})
	if len(pruned) > recentOutboundMaxEntries {
		pruned = pruned[len(pruned)-recentOutboundMaxEntries:]
	}
	s.recentOutboundTexts = pruned
}

// IsRecentOutboundChat reports whether Device sent this text recently.
func (s *CodexService) IsRecentOutboundChat(text string) bool {
	if text == "" {
		return false
	}
	now := time.Now().UnixMilli()
	cutoff := now - recentOutboundWindowMs
	s.recentOutboundMu.Lock()
	defer s.recentOutboundMu.Unlock()
	for _, r := range s.recentOutboundTexts {
		if r.ts >= cutoff && r.text == text {
			return true
		}
	}
	return false
}
