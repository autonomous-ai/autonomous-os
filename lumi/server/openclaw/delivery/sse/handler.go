package sse

import (
	"strings"
	"sync"
	"sync/atomic"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/monitor"
	"go-lamp.autonomous.ai/internal/statusled"
	"go-lamp.autonomous.ai/lib/flow"
	"go-lamp.autonomous.ai/lib/mood"
	"go-lamp.autonomous.ai/lib/musicsuggestion"
	"go-lamp.autonomous.ai/lib/posture"
	"go-lamp.autonomous.ai/lib/wellbeing"
	"go-lamp.autonomous.ai/server/config"
)

// OpenClawHandler handles OpenClaw gateway WebSocket events and exposes monitor endpoints.
type OpenClawHandler struct {
	agentGateway domain.AgentGateway
	monitorBus   *monitor.Bus
	statusLED    *statusled.Service

	// assistantBuf accumulates assistant deltas per runId so we can send the
	// full text to TTS when the agent turn ends (lifecycle "end").
	assistantMu  sync.Mutex
	assistantBuf map[string]*strings.Builder

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
	// sensing trace (race: flowRunID becomes lumi-sensing-* → isChannelRun false).
	channelRunsMu sync.Mutex
	channelRuns   map[string]bool

	// interleavedDMByRunID captures Telegram chat_ids when a Telegram message
	// is injected mid-turn into a Lumi-issued run (queue mode). At lifecycle.end
	// the reply is routed back to that chat instead of TTS — fixes "Lumi
	// answered Telegram question on the speaker" when sensing/voice was the
	// run originator. Protected by channelRunsMu.
	interleavedDMByRunID map[string]string

	// cronFireRuns tracks runs initiated by an OpenClaw scheduled cron fire.
	// Populated when a lifecycle_start (UUID runId, no lumi- prefix) arrives
	// shortly after an event:"cron" (action:"started") — OpenClaw's cron
	// event omits sessionKey for sessionTarget="main" jobs, so we can't
	// correlate by session and instead consume from a FIFO timestamp queue.
	// Membership forces isChannelRun=false so the lamp speaker fires.
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
	// isControlUiVisible, so non-Lumi-originated runs receive only
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

	// compacting prevents duplicate /compact sends while one is in progress.
	compacting atomic.Bool

	// newSessioning prevents duplicate sessions.new sends while one is
	// in flight. Cooldown is shorter than compacting because new-session
	// completes server-side instantly.
	newSessioning atomic.Bool
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

// ProvideOpenClawHandler returns an OpenClaw events handler.
func ProvideOpenClawHandler(gw domain.AgentGateway, bus *monitor.Bus, sled *statusled.Service) OpenClawHandler {
	// Init flow emitter here so ws_connect events (fired from StartWS before any HTTP request)
	// are broadcast to SSE. Lumi is a single-user device so the global trace ID is sufficient;
	// concurrent turn interleaving is not a concern in normal operation.
	flow.Init(bus, config.LumiVersion)
	mood.Init()
	wellbeing.Init()
	musicsuggestion.Init()
	posture.Init()
	// Populate OpenClaw version cache in the background so the first Status
	// poll has it ready. Stays in package-level state because the handler
	// struct is returned by value through wire — capturing &h.field here
	// would write to a soon-to-be-discarded copy.
	go populateOpenClawVersion()
	return OpenClawHandler{
		agentGateway:         gw,
		monitorBus:           bus,
		statusLED:            sled,
		assistantBuf:         make(map[string]*strings.Builder),
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
func (h *OpenClawHandler) IsSleeping() bool {
	h.lastEmotionMu.Lock()
	defer h.lastEmotionMu.Unlock()
	return h.lastEmotion == "sleepy"
}

// consumeInterleavedDM atomically reads and removes the captured Telegram
// chat_id for runID. Empty result means no interleaved Telegram message was
// recorded for this turn — the normal TTS path applies.
func (h *OpenClawHandler) consumeInterleavedDM(runID string) string {
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
