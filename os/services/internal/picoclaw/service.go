// Package picoclaw implements domain.AgentGateway against a PicoClaw runtime
// reached over a persistent WebSocket. See docs/agentic/picoclaw.md for the protocol
// mapping and the runtime boundaries with OpenClaw / Hermes.
//
// PicoClaw is assumed to be running locally on the Pi as a systemd service at
// WSURL with all skills already provisioned. os-server only acts as a client:
// it opens one persistent socket, sends user turns as `message.send`, and
// translates the inbound frames (typing.*, message.create/update/delete, error,
// pong) into the same domain.WSEvent shape that the OpenClaw handler at
// server/agent/delivery/http/handler_events.go consumes — so the downstream
// pipeline (HAL TTS, [HW:/...] markers, monitor SSE, sensing drain, Telegram
// fan-out) stays untouched.
//
// Unlike OpenClaw, PicoClaw does not stream tokens and has no challenge/pairing
// handshake: the answer to a turn arrives whole in one final frame, and there is
// no per-frame runId on the wire. Turns are therefore correlated by a single
// in-flight runID (PicoClaw processes one turn at a time).
package picoclaw

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

// Compile-time check: *PicoclawService implements domain.AgentGateway.
var _ domain.AgentGateway = (*PicoclawService)(nil)

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

// PicoclawService is the PicoClaw backend implementation of domain.AgentGateway.
type PicoclawService struct {
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

	// Session state. sessionUUID is the server-assigned session_id captured from
	// any inbound frame.
	sessionUUID atomic.Value // string

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

// ProvideService constructs the PicoClaw service. Wired via internal/agent/factory.go
// when config.AgentRuntime == "picoclaw".
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *PicoclawService {
	s := &PicoclawService{
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
func (s *PicoclawService) Name() string { return "PicoClaw" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *PicoclawService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *PicoclawService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — PicoClaw does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle. Returns 0 (unknown).
func (s *PicoclawService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.PicoclawService. Used by the
// session.message handler to skip echoes of Device-injected user messages.
func (s *PicoclawService) markOutboundChat(text string) {
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
func (s *PicoclawService) IsRecentOutboundChat(text string) bool {
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
