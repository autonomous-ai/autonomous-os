package openclaw

import (
	"encoding/json"
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

const (
	defaultGatewayWSURL = "ws://127.0.0.1:18789"
	customProviderName  = "autonomous"
	// autonomousProviderAPI is the FALLBACK wire protocol for the "autonomous"
	// provider, used only when the models API response omits its `api` field
	// (or on the hardcoded-fallback setup path). The autonomous gateway speaks a
	// single protocol regardless of which model it routes to, so this is never
	// derived per-model from the model name.
	autonomousProviderAPI = "anthropic-messages"
	defaultGatewayMode    = "local"
	defaultGatewayBind    = "loopback"
	defaultGatewayPort    = 18789
	openclawRuntimeUser   = "root"
)

// Compile-time check: *OpenclawService implements domain.AgentGateway.
var _ domain.AgentGateway = (*OpenclawService)(nil)

// reSnapshotPath matches [snapshot: /path/to/file.jpg] markers in sensing messages.
var reSnapshotPath = regexp.MustCompile(`\[snapshot:\s*[^\]]+\]`)

// Pose bucket markers — emitted by hal motion.py on motion.activity
// when a posture nudge folds in. Drain path strips them before forwarding
// to the LLM and extracts the (bucket_id, worst_filenames) pair so the SSE
// /dm path can attach the worst frames to the Telegram DM. Mirrors the
// regex pair in server/sensing/delivery/http/handler.go.
var rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
var rePoseWorstMarker = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)

// extractPoseBucketMarkers pulls (bucket_id, filenames) from a sensing
// message. Returns ("", nil) when there's no bucket marker.
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

// OpenclawService provides setup, reset, restart of openclaw config/gateway and StartWS.
type OpenclawService struct {
	config        *config.Config
	monitorBus    *monitor.Bus
	statusLED     *statusled.Service
	wsConnected   atomic.Bool  // true when gateway WebSocket is connected and ready to receive messages
	wsConnectedAt atomic.Int64 // unix seconds when wsConnected last flipped to true; 0 when disconnected
	// agentStartedAt is the unix-seconds timestamp the OpenClaw gateway process
	// started, derived from the server.uptimeMs field of the hello-ok response
	// at handshake. Survives os-server restarts because each fresh hello-ok carries
	// the gateway's own age. 0 when not yet observed or disconnected.
	agentStartedAt atomic.Int64
	activeTurn     atomic.Bool  // true while agent is processing a turn (lifecycle start → end)
	busySince      atomic.Int64 // unix milli when activeTurn was last set to true; used to expire stuck busy state
	wsHasConnected atomic.Bool  // true after first successful WS connect (skip reconnect TTS on boot)

	// wsConn is the active WebSocket connection; guarded by wsMu.
	wsConn *websocket.Conn
	wsMu   sync.Mutex
	// lastSessionKey is the most recent session key observed from agent lifecycle events.
	lastSessionKey atomic.Value // string
	// reqCounter is used to generate unique request IDs for outgoing RPC calls.
	reqCounter atomic.Int64

	// pendingRPC tracks in-flight RPC requests waiting for a response.
	pendingRPCMu sync.Mutex
	pendingRPC   map[string]chan json.RawMessage // reqID → response channel

	// pendingEvents buffers sensing events received while agent is busy.
	// All events are kept (no dedup) — motion/presence must not be missed. Drained on SetBusy(false).
	pendingEventsMu sync.Mutex
	pendingEvents   []pendingEvent

	// guardRuns tracks runIDs that are guard-active sensing turns.
	// When the agent responds, the SSE handler broadcasts the response via Telegram Bot API.
	guardRunsMu sync.Mutex
	guardRuns   map[string]string // runID → snapshot path

	// channels is the list of registered messaging channel senders (Telegram, Discord, Slack, etc.).
	channels []domain.ChannelSender

	// broadcastRuns tracks runIDs whose agent response should be broadcast
	// to all messaging channels alongside TTS (e.g. music.mood confirmations).
	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	// webChatRuns tracks runIDs originating from the web monitor chat.
	// TTS is suppressed for these runs — response is displayed in the web UI only.
	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	// silentRuns tracks runIDs whose spoken reply must be suppressed even though
	// the agent still processes the turn. Used for voice_agent_handled: the
	// realtime voice agent already answered the user, so OpenClaw absorbs the
	// exchange for memory/mood (input-branching SKILL.md rule #3) but must not
	// speak it again. Suppression skips the TTS POST entirely (no synth, no cost —
	// same as web chat / Telegram), and is a deterministic backstop for that
	// skill's NO_REPLY, which the LLM can ignore.
	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	// poseBucketRuns associates a motion.activity runID with the hal
	// pose bucket whose window just fired. Populated by the sensing
	// handler when it parses [pose_bucket:...] / [pose_worst:...] markers,
	// consumed by the SSE /dm path so the worst frames can ride along on
	// the Telegram DM without the agent needing to know file paths.
	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// primarySyncMu serialises syncPrimaryFromFile() invocations so concurrent
	// debounce-timer firings and UpdatePrimaryModel calls don't race on
	// s.config.LLMModel + config.Save().
	primarySyncMu sync.Mutex

	// pendingChat tracks outbound chat.sends not yet paired with a lifecycle.
	// Each entry stores the idempotencyKey, the exact message text, and send
	// time. UUID → idempotencyKey mapping is done by matching the OpenClaw
	// agent's last user message (fetched via chat.history) against stored text
	// — see MatchPendingByMessage. No FIFO ordering: the message content is
	// the strong key, which holds even when OpenClaw drains the followup
	// queue out of send order or drops a turn entirely.
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace

	// recentOutboundTexts is a small ring buffer of message texts the os server
	// sent via chat.send (wake greeting, ambient guard, sensing events). Used by
	// the session.message SSE handler to skip echoes — OpenClaw rebroadcasts
	// every chat.send-injected message as session.message role=user, which
	// is indistinguishable from real channel input on shape alone.
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound
}

type recentOutbound struct {
	text string
	ts   int64 // unix ms
}

const recentOutboundWindowMs int64 = 30_000
const recentOutboundMaxEntries = 32

// pendingTrace pairs a chat.send idempotencyKey with the message text and
// send time. Matching is by message text (via MatchPendingByMessage) so the
// OpenClaw UUID lifecycle drained from the followup queue resolves back to
// the correct device runId without relying on send-order FIFO.
type pendingTrace struct {
	runID   string
	message string
	sentAt  time.Time
}

// poseBucketInfo carries the hal bucket identifier and the pre-selected
// worst-snapshot filenames for a single motion.activity turn. Filenames
// stay raw (no path prefix) so the consumer can rebuild paths against
// whichever snapshot tmp dir applies.
type poseBucketInfo struct {
	bucketID  string
	filenames []string
	markedAt  time.Time
}

// ProvideService constructs the openclaw service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *OpenclawService {
	s := &OpenclawService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		pendingRPC:     make(map[string]chan json.RawMessage),
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	// Register channel senders.
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
	}
	return s
}

// Name returns the display name of this agent gateway.
func (s *OpenclawService) Name() string {
	return "OpenClaw"
}

// Version returns the cached OpenClaw binary version (e.g. "2026.5.27"), or empty
// when undetected. Satisfies domain.AgentGateway.Version().
func (s *OpenclawService) Version() string {
	return GetOpenClawVersion()
}

// markOutboundChat records an os-server-sent chat.send message text so the SSE
// session.message handler can skip its echo. Trims expired + over-cap.
func (s *OpenclawService) markOutboundChat(text string) {
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

// IsRecentOutboundChat reports whether the os server sent this text recently.
// Match is exact on the message string the os server passes to chat.send (after sensing
// snapshot path stripping — caller needs to compare against the same form).
func (s *OpenclawService) IsRecentOutboundChat(text string) bool {
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

// IsReady returns true when the gateway WebSocket is connected and OpenClaw is ready to receive messages.
func (s *OpenclawService) IsReady() bool {
	return s.wsConnected.Load()
}

// ConnectedAt returns the unix-seconds timestamp when the WS connection last
// became ready, or 0 when not currently connected.
func (s *OpenclawService) ConnectedAt() int64 {
	return s.wsConnectedAt.Load()
}

// AgentUptime returns the OpenClaw gateway process uptime in seconds, derived
// from server.uptimeMs in the hello-ok response. Independent of the os server's
// WS reconnect cycles — restarting the os server does not reset this value. Returns 0 when
// the gateway uptime has not yet been observed or the WS is disconnected.
func (s *OpenclawService) AgentUptime() int64 {
	if !s.wsConnected.Load() {
		return 0
	}
	startedAt := s.agentStartedAt.Load()
	if startedAt <= 0 {
		return 0
	}
	uptime := time.Now().Unix() - startedAt
	if uptime < 0 {
		return 0
	}
	return uptime
}
