package http

import (
	"strings"
	"sync"
	"sync/atomic"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/internal/monitor"
	"go.autonomous.ai/os/internal/statusled"
	"go.autonomous.ai/os/lib/flow"
	"go.autonomous.ai/os/lib/mood"
	"go.autonomous.ai/os/lib/musicsuggestion"
	"go.autonomous.ai/os/lib/posture"
	"go.autonomous.ai/os/lib/wellbeing"
	"go.autonomous.ai/os/server/config"
)

// AgentHandler handles OpenClaw gateway WebSocket events and exposes monitor endpoints.
type AgentHandler struct {
	agentGateway domain.AgentGateway
	monitorBus   *monitor.Bus
	statusLED    *statusled.Service
	config       *config.Config // device type → capability gate for agent HW markers

	// assistantBuf accumulates assistant deltas per runId so we can send the
	// full text to TTS when the agent turn ends (lifecycle "end").
	//
	// streamedCleanLen tracks bytes of the HW-stripped reply already
	// dispatched to TTS by trySentenceFlush. Only the FIRST sentence is
	// streamed mid-turn — chaining each sentence as its own /voice/speak
	// POST produced a ~400ms TTFB gap between sentences (choppy). The
	// remainder goes through /voice/speak-queue at lifecycle:end, which
	// Python pre-synthesises while sentence 1 is still playing so the rest
	// of the reply chains on with no audible gap. Shares assistantMu.
	assistantMu      sync.Mutex
	assistantBuf     map[string]*strings.Builder
	streamedCleanLen map[string]int
	// ADDED 2026-05-26: count of leading HW markers fired at stream-time per
	// runID. Used at lifecycle:end to skip already-fired markers (avoid
	// double-fire). Cleared on lifecycle:end / channel-turn finalize. Shares
	// assistantMu — same per-runID scope as the buffer it tracks markers in.
	firedHWCount map[string]int

	// ttsSuppressReasons tracks runIDs that should skip TTS on lifecycle end.
	// Value is the reason: "music_playing" (speaker shared with audio) or
	// "already_spoken" (TTS tool intercepted and already routed to speaker).
	ttsSuppressMu      sync.Mutex
	ttsSuppressReasons map[string]string

	// runIDMap maps OpenClaw-assigned UUIDs back to device-originated idempotencyKeys.
	// When lifecycle_start arrives with UUID while a device trace is active, we store
	// the mapping so all subsequent events for that UUID use the device ID for flow tracing.
	runIDMapMu sync.Mutex
	runIDMap   map[string]string // OpenClaw UUID → device idempotencyKey

	// lastEmotion tracks the most recent emotion expressed by the agent.
	lastEmotionMu sync.Mutex
	lastEmotion   string

	// channelRuns tracks runs confirmed from a real channel user (Telegram/etc.)
	// via senderLabel. Prevents TTS when a Telegram UUID gets mapped to a
	// sensing trace (race: flowRunID becomes device-sensing-* → isChannelRun false).
	channelRunsMu sync.Mutex
	channelRuns   map[string]bool

	// interleavedDMByRunID captures Telegram chat_ids when a Telegram message
	// is injected mid-turn into a device-issued run (queue mode). At lifecycle.end
	// the reply is routed back to that chat instead of TTS — fixes "the device
	// answered Telegram question on the speaker" when sensing/voice was the
	// run originator. Protected by channelRunsMu.
	interleavedDMByRunID map[string]string

	// cronFireRuns tracks runs initiated by an OpenClaw scheduled cron fire.
	// Populated when a lifecycle_start (UUID runId, no lamp- prefix) arrives
	// shortly after an event:"cron" (action:"started") — OpenClaw's cron
	// event omits sessionKey for sessionTarget="main" jobs, so we can't
	// correlate by session and instead consume from a FIFO timestamp queue.
	// Membership forces isChannelRun=false so the device speaker fires.
	cronFireRunsMu sync.Mutex
	cronFireRuns   map[string]bool

	// cronFireExpected is a FIFO queue of unix-ms timestamps from recent
	// cron "started" events. Each lifecycle_start with a UUID runId
	// consumes the oldest entry if it falls within cronFireWindowMs.
	// Stale entries (older than the window) are pruned on each access.
	cronFireExpectedMu sync.Mutex
	cronFireExpected   []int64

	// channelTurns tracks active channel-initiated turns (Telegram, etc.) keyed
	// by sessionKey. OpenClaw 5.x gates the `agent` lifecycle stream behind
	// isControlUiVisible, so non-device-originated runs receive only
	// `session.message` / `session.tool` / `sessions.changed`. chat_input,
	// lifecycle synthesis, and HW marker firing for those turns must be
	// driven from `session.message` here. Each entry holds the synthetic
	// device runId, accumulated assistant text, and turn metadata.
	channelTurnMu sync.Mutex
	channelTurns  map[string]*channelTurnState

	// agentLifecycleAt tracks when an `event=agent` lifecycle.start last fired
	// per sessionKey. Used by the session.message handler to skip turns that
	// are already being driven by the agent path (cron heartbeat fires both
	// streams; real user telegram fires only session.message).
	//
	// activeRunIDBySession tracks the in-flight runID per session so the
	// session.message handler can attribute interleaved channel messages to
	// the running turn even when the message itself is skipped.
	agentLifecycleMu     sync.Mutex
	agentLifecycleAt     map[string]int64
	activeRunIDBySession map[string]string

	// streamStats tracks per-run streaming counters and accumulated text for
	// JSONL emission of agent_first_token / agent_last_token (assistant
	// stream) and thinking_first_token / thinking_last_token (extended
	// thinking stream). Live deltas already flow through monitorBus but the
	// JSONL persist layer drops them — these summary events are the
	// persisted projection that Flow Monitor renders from on reload.
	streamStatsMu sync.Mutex
	streamStats   map[string]*runStreamStats

	// compacting prevents duplicate /compact sends while one is in progress.
	compacting atomic.Bool

	// newSessioning prevents duplicate sessions.new sends while one is
	// in flight. Cooldown is shorter than compacting because new-session
	// completes server-side instantly.
	newSessioning atomic.Bool
}

// runStreamStats is the per-run streaming bookkeeping that backs the
// agent_*_token / thinking_*_token JSONL events. Independent of assistantBuf
// (which serves TTS flush) so the two paths can't interfere.
type runStreamStats struct {
	assistantFirstSeen bool
	assistantChunks    int
	assistantChars     int
	assistantText      strings.Builder

	thinkingFirstSeen bool
	thinkingChunks    int
	thinkingChars     int
	thinkingText      strings.Builder
}

// channelTurnState tracks the in-flight assistant response for a channel
// session (Telegram/etc.) so HW markers in the final assistant message can
// be extracted and fired even when no `agent` lifecycle event arrives.
type channelTurnState struct {
	runID       string
	senderLabel string
	telegramID  string
	accumulated strings.Builder
	startedAtMs int64
}

// cronFireWindowMs is the max delay between an OpenClaw cron "started" event
// and the lifecycle_start it precedes. Observed ~2s in practice; 10s leaves
// generous headroom for slow/loaded runs without false-positive correlations.
const cronFireWindowMs int64 = 10_000

// ProvideAgentHandler returns an OpenClaw events handler.
func ProvideAgentHandler(gw domain.AgentGateway, bus *monitor.Bus, sled *statusled.Service, cfg *config.Config) AgentHandler {
	// Init flow emitter here so ws_connect events (fired from StartWS before any HTTP request)
	// are broadcast to SSE. The device is single-user so the global trace ID is sufficient;
	// concurrent turn interleaving is not a concern in normal operation.
	flow.Init(bus, config.OSVersion)
	mood.Init()
	wellbeing.Init()
	musicsuggestion.Init()
	posture.Init()
	// Populate OpenClaw version cache in the background so the first Status
	// poll has it ready. Stays in package-level state because the handler
	// struct is returned by value through wire — capturing &h.field here
	// would write to a soon-to-be-discarded copy.
	go populateOpenClawVersion()
	go populateHermesVersion()
	go populatePicoclawVersion()
	return AgentHandler{
		agentGateway:         gw,
		monitorBus:           bus,
		statusLED:            sled,
		config:               cfg,
		assistantBuf:         make(map[string]*strings.Builder),
		streamedCleanLen:     make(map[string]int),
		firedHWCount:         make(map[string]int),
		streamStats:          make(map[string]*runStreamStats),
		ttsSuppressReasons:   make(map[string]string),
		runIDMap:             make(map[string]string),
		channelRuns:          make(map[string]bool),
		interleavedDMByRunID: make(map[string]string),
		cronFireRuns:         make(map[string]bool),
		channelTurns:         make(map[string]*channelTurnState),
		agentLifecycleAt:     make(map[string]int64),
		activeRunIDBySession: make(map[string]string),
	}
}

// IsSleeping returns true when the last emotion expressed by the agent was "sleepy".
// Used by SensingHandler to suppress passive sensing events during sleep mode.
func (h *AgentHandler) IsSleeping() bool {
	h.lastEmotionMu.Lock()
	defer h.lastEmotionMu.Unlock()
	return h.lastEmotion == "sleepy"
}

// consumeInterleavedDM atomically reads and removes the captured Telegram
// chat_id for runID. Empty result means no interleaved Telegram message was
// recorded for this turn — the normal TTS path applies.
func (h *AgentHandler) consumeInterleavedDM(runID string) string {
	if runID == "" {
		return ""
	}
	h.channelRunsMu.Lock()
	defer h.channelRunsMu.Unlock()
	cid := h.interleavedDMByRunID[runID]
	if cid != "" {
		delete(h.interleavedDMByRunID, runID)
	}
	return cid
}
