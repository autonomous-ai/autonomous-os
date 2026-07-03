// Package claudecode implements domain.AgentGateway against Claude Code
// (the Anthropic CLI agent) reached over a persistent WebSocket to a thin local
// bridge. See docs/agentic/claudecode.md for the protocol mapping and the
// runtime boundaries with OpenClaw / Hermes / PicoClaw.
//
// Claude Code has no server mode, so the claudecode systemd unit runs a bridge
// (presync-materialized /root/.claudecode/bridge.py) that holds ONE headless
// Claude process (`claude --print --input-format stream-json --output-format
// stream-json`, plus `--channels plugin:telegram@...` when configured) and
// exposes the socket at WSURL. os-server only acts as a client: it sends user
// turns as `message.send`, and translates the forwarded Claude stream-json
// events (system / assistant / user / result) into the same domain.WSEvent
// shape that the OpenClaw handler at server/agent/delivery/http/
// handler_events.go consumes — so the downstream pipeline (HAL TTS, [HW:/...]
// markers, monitor SSE, sensing drain, Telegram fan-out) stays untouched.
//
// Like PicoClaw, there is no per-frame runId on the wire: the final answer
// arrives on the `result` event, and turns are correlated by a single in-flight
// runID (the bridge's Claude process handles one turn at a time; queued inputs
// are serialized by Claude itself).
package claudecode

import (
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/monitor"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/server/config"
)

// Compile-time check: *ClaudeCodeService implements domain.AgentGateway.
var _ domain.AgentGateway = (*ClaudeCodeService)(nil)

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

// ClaudeCodeService is the Claude Code backend implementation of domain.AgentGateway.
type ClaudeCodeService struct {
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

	// lastAssistantText is the latest assistant text block of the in-flight
	// turn — the fallback final text when result.result is empty (translator.go).
	lastAssistantText atomic.Value // string

	// mcpMu serializes workspace/.mcp.json read-modify-write cycles (mcp.go).
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

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// Channel senders (Telegram).
	channels []domain.ChannelSender

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

// ProvideService constructs the Claude Code service. Wired via internal/agent/factory.go
// when config.AgentRuntime == "claudecode".
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *ClaudeCodeService {
	s := &ClaudeCodeService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
	}
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *ClaudeCodeService) Name() string { return "Claude Code" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *ClaudeCodeService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *ClaudeCodeService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — Claude Code does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle. Returns 0 (unknown).
func (s *ClaudeCodeService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.ClaudeCodeService. Used by the
// session.message handler to skip echoes of Device-injected user messages.
func (s *ClaudeCodeService) markOutboundChat(text string) {
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
func (s *ClaudeCodeService) IsRecentOutboundChat(text string) bool {
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
