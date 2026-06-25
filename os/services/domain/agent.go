package domain

import (
	"context"
	"encoding/json"
)

// TelegramTarget represents a Telegram chat the bot is connected to.
type TelegramTarget struct {
	ChatID string // e.g. "158406741" (DM) or "-5179782244" (group)
	Type   string // "private", "group", "supergroup", "channel"
}

// ChannelSender delivers messages to a specific messaging channel (Telegram, Discord, Slack, etc.).
type ChannelSender interface {
	// Name returns the channel name (e.g. "telegram", "discord", "slack").
	Name() string

	// IsConfigured returns true if this channel has valid credentials/config.
	IsConfigured() bool

	// Send delivers a message with an optional image to all targets in this channel.
	Send(msg string, imagePath string) error
}

// AgentEventHandler processes events from an agent gateway connection.
type AgentEventHandler func(ctx context.Context, evt WSEvent) error

// AgentGateway abstracts an agentic runtime (OpenClaw, PicoClaw, etc.).
type AgentGateway interface {
	// Name returns the display name of this agent gateway (e.g. "OpenClaw", "PicoClaw").
	Name() string

	// Version returns the active backend's own version (e.g. OpenClaw "2026.5.27",
	// Hermes "0.17.0"), so version surfaces (web Status, etc.) reflect the running
	// runtime rather than always reporting OpenClaw. Empty when undetected.
	Version() string

	// IsReady returns true when the agent runtime is connected and ready.
	IsReady() bool

	// ConnectedAt returns the unix-seconds timestamp when the runtime
	// connection last became ready, or 0 when not currently connected.
	ConnectedAt() int64

	// AgentUptime returns the agent runtime process uptime in seconds,
	// independent of the local WS reconnect cycle. Returns 0 when not connected
	// or when the gateway has not yet reported its uptime.
	AgentUptime() int64

	// IsBusy returns true when the agent is currently processing a turn.
	// Passive sensing events should be dropped while busy to avoid interrupting active commands.
	IsBusy() bool

	// SetBusy marks the agent as busy (true on lifecycle start, false on lifecycle end).
	SetBusy(busy bool)

	// QueuePendingEvent buffers a sensing event to replay when the agent becomes idle.
	// Last-write-wins per event type. fixedRunID lets web_chat preallocate the runID
	// returned to the web client so it can correlate SSE events at replay time;
	// other event types pass "" and a fresh runID is allocated at drain.
	QueuePendingEvent(eventType, msg, image, fixedRunID string)

	// SendChatMessage sends a user message to the agent. Returns the run ID.
	SendChatMessage(msg string) (string, error)

	// SendSystemChatMessage sends a system-originated message (skill updates, wake greeting,
	// /compact) so Flow Monitor can render it separately from real user input. The WS RPC
	// payload is identical to SendChatMessage.
	SendSystemChatMessage(msg string) (string, error)

	// SendChatMessageWithImage sends a message with a base64 JPEG image attachment.
	// Used by sensing events that include a camera snapshot for AI vision analysis.
	SendChatMessageWithImage(msg string, imageBase64 string) (string, error)

	// NextChatRunID allocates the chat request id and idempotency key for the next outbound chat.send.
	// Call flow.SetTrace(runID) before flow.Start so the sensing_input enter line matches chat_send.
	NextChatRunID() (reqID string, runID string)

	// SendChatMessageWithRun sends using a preallocated pair from NextChatRunID (same idempotency as chat.send).
	SendChatMessageWithRun(msg string, reqID string, runID string) (string, error)

	// SendChatMessageWithImageAndRun is SendChatMessageWithImage with preallocated ids.
	SendChatMessageWithImageAndRun(msg string, imageBase64 string, reqID string, runID string) (string, error)

	// SendSlashCommandWithRun sends a slash-prefixed message (e.g. "/status")
	// with deliver:false so the gateway routes the reply only back to this
	// chat.send caller (mirrors gw web behavior). Use when the message text
	// starts with "/" and originates from the web monitor chat.
	SendSlashCommandWithRun(msg string, reqID string, runID string) (string, error)

	// SendSlashCommandWithImageAndRun is SendSlashCommandWithRun with image attachment.
	SendSlashCommandWithImageAndRun(msg string, imageBase64 string, reqID string, runID string) (string, error)

	// GetSessionKey returns the current agent session key, or empty string.
	GetSessionKey() string

	// SetSessionKey stores the session key for outgoing messages.
	SetSessionKey(key string)

	// SetupAgent configures and starts the agent runtime from setup data.
	SetupAgent(data SetupRequest) error

	// SupportedChannels returns the messaging channels this runtime can run
	// (e.g. "telegram", "slack"). Callers enforce supported-vs-not-supported before
	// persisting credentials and decide what to re-apply on a runtime switch.
	// openclaw: telegram/slack/discord/whatsapp; hermes: telegram/slack/discord
	// (delivered by the Hermes server via ~/.hermes/.env); picoclaw: telegram.
	SupportedChannels() []string

	// AddChannel adds a messaging channel to the agent runtime. ctx caps the
	// underlying CLI subprocess + restart so callers (MQTT 10-min budget) can
	// bound the whole flow. WhatsApp pairing is a separate streaming call —
	// PairWhatsapp; this method only writes the channel config + enables the
	// plugin.
	AddChannel(ctx context.Context, data AddChannelRequest) error

	// RefreshChannelConfig re-applies the canonical channels.<channel> block in
	// openclaw.json using the same writer AddChannel uses, then restarts the
	// gateway. Unlike AddChannel this path is config-only: no plugin install, no
	// CLI bootstrap, no pairing. Returns the detected runtime version string
	// ("Y.M.P", empty when undetected) so callers can echo it in fd_channel
	// responses, and errors out (without restarting) when openclaw.json does not
	// yet exist — refresh is only meaningful on already-onboarded devices. Today
	// only the slack channel is implemented; other channels return an error.
	RefreshChannelConfig(ctx context.Context, req RefreshChannelRequest) (runtime string, err error)

	// HasWhatsappSession reports whether a Baileys session already exists on
	// disk for the given account ("default" when empty). When true, AddChannel
	// callers can emit a single PairingStatusSuccess event and skip the
	// interactive QR pairing flow.
	HasWhatsappSession(account string) bool

	// PairWhatsapp runs `openclaw channels login --channel whatsapp` and emits
	// PairingEvents on the returned channel. Callers MUST drain. Only one
	// pairing flow may be active per device; concurrent calls produce a
	// one-event channel containing PairingStatusFailure.
	PairWhatsapp(ctx context.Context) <-chan PairingEvent

	// ResetAgent factory-resets the agent runtime configuration.
	ResetAgent() error

	// RestartAgent restarts the agent runtime process.
	RestartAgent() error

	// RefreshModelsConfig patches the models reasoning fields in openclaw.json
	// based on the current LLMDisableThinking config and restarts the agent.
	RefreshModelsConfig() error

	// EnsureOnboarding seeds personality/identity files into the agent workspace.
	EnsureOnboarding() error

	// FetchChatHistory sends a chat.history RPC and returns the raw messages array.
	// Best-effort: returns nil on error or timeout without failing the caller.
	FetchChatHistory(sessionKey string, limit int) (json.RawMessage, error)

	// GetConfigJSON returns the raw openclaw.json bytes.
	GetConfigJSON() (json.RawMessage, error)

	// WriteMCPEntry upserts mcp.servers.<name> in openclaw.json (the server
	// config map, e.g. {type, url, headers}) and restarts the gateway. Used by
	// the connector.set MQTT flow to wire remote-MCP connectors.
	WriteMCPEntry(name string, entry map[string]any) error

	// RemoveMCPEntry deletes mcp.servers.<name>. Returns removed=false (no
	// write/restart) when the entry was already absent.
	RemoveMCPEntry(name string) (bool, error)

	// StartWS connects to the agent runtime and runs the event read loop.
	StartWS(ctx context.Context, handler AgentEventHandler)

	// MarkGuardRun marks a runID as a guard-active turn. When the agent responds,
	// the SSE handler will broadcast the response to all Telegram chats via Bot API.
	MarkGuardRun(runID string, snapshotPath string)

	// ConsumeGuardRun checks if a runID is a guard-active turn and returns the
	// snapshot path. Returns ("", false) if not a guard run.
	ConsumeGuardRun(runID string) (snapshotPath string, ok bool)

	// MarkBroadcastRun marks a runID so the agent's response is broadcast
	// to all messaging channels alongside TTS. Used for music.mood confirmations
	// and other events where the user should be able to respond via voice or channel.
	MarkBroadcastRun(runID string)

	// ConsumeBroadcastRun checks if a runID is marked for broadcast. One-shot.
	ConsumeBroadcastRun(runID string) bool

	// MarkPoseBucketRun stashes the pose bucket + worst-snapshot filenames
	// associated with a motion.activity turn that surfaced a posture nudge.
	// The SSE handler consumes this on /dm so the worst frames can be
	// attached to the Telegram message without the agent having to know
	// any file paths. bucketID is hal's window_start integer; filenames
	// are relative to <SNAPSHOT_TMP_DIR>/sensing_pose/buckets/<bucketID>/.
	MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string)

	// ConsumePoseBucketRun returns + removes the pose bucket info for a
	// runID. One-shot, mirrors ConsumeGuardRun. ok is false when the run
	// has no associated bucket (most turns).
	ConsumePoseBucketRun(runID string) (bucketID string, worstFilenames []string, ok bool)

	// MarkWebChatRun marks a runID as originating from the web monitor chat.
	// TTS is suppressed for these runs — response is displayed in the web UI only.
	MarkWebChatRun(runID string)

	// IsWebChatRun checks if a runID is a web chat run (non-consuming).
	IsWebChatRun(runID string) bool

	// ConsumeWebChatRun checks and removes a web-chat-marked runID. One-shot.
	ConsumeWebChatRun(runID string) bool

	// MarkSilentRun marks a runID whose spoken reply must be suppressed even
	// though the agent still processes the turn (e.g. voice_agent_handled: the
	// realtime voice agent already replied, so the body stays silent but still
	// absorbs the exchange for memory).
	MarkSilentRun(runID string)

	// IsSilentRun checks if a runID is a silent run (non-consuming).
	IsSilentRun(runID string) bool

	// ConsumeSilentRun checks and removes a silent-marked runID. One-shot.
	ConsumeSilentRun(runID string) bool

	// SetPendingChatTrace records the idempotencyKey and exact message text of
	// an outbound chat.send so a later UUID lifecycle (drained from OpenClaw's
	// followup queue, which strips the idempotencyKey) can be mapped back via
	// MatchPendingByMessage. The message string must match what was sent on
	// the WS — chat.history returns it verbatim.
	SetPendingChatTrace(runID string, message string)

	// RemovePendingChatTraceByRunID removes the entry whose runID matches
	// target. Used when lifecycle_start arrives with a device-format runId
	// (5.4+ echo path) — the runId IS the device trace, no mapping needed,
	// but the entry must be cleared so MatchPendingByMessage doesn't pick
	// it up for a later UUID lifecycle with the same message.
	RemovePendingChatTraceByRunID(target string) bool

	// MatchPendingByMessage finds and removes the pending entry whose stored
	// message text matches needle. Used when a UUID lifecycle arrives: the
	// device fetches chat.history, extracts the last user message, and calls this to
	// recover the original idempotencyKey. Returns "" when no entry matches.
	MatchPendingByMessage(needle string) string

	// --- Channel abstraction (backend-agnostic) ---

	// GetTelegramBotToken returns the Telegram bot token used by the agent runtime.
	GetTelegramBotToken() string

	// GetTelegramTargets returns all Telegram chats (DMs + groups) the bot is connected to.
	GetTelegramTargets() ([]TelegramTarget, error)

	// Broadcast sends a message to all connected messaging channels.
	// Currently supports Telegram via Bot API. imagePath is an optional local image file.
	Broadcast(msg string, imagePath string) error

	// SendToUser sends a direct message to a specific Telegram user by their user ID.
	// If the user ID is empty, the message is silently dropped.
	SendToUser(telegramID string, msg string, imagePath string) error

	// SendToUserWithMedia sends a DM with multiple images via Telegram's
	// sendMediaGroup (caption rides on the first photo). When imagePaths
	// is empty or a single entry, behavior reduces to SendToUser. Telegram
	// caps sendMediaGroup at 10 photos; callers should self-limit.
	SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error

	// SendToHALTTS posts response text to HAL for TTS playback.
	SendToHALTTS(text string) error

	// SendToHALTTSQueue posts text to /voice/speak-queue: plays
	// immediately when idle, otherwise queues + pre-synthesizes so the audio
	// chains seamlessly onto the current speech (used for sentence-streamed
	// agent replies).
	SendToHALTTSQueue(text string) error

	// StopTTS interrupts active TTS playback and music on HAL.
	StopTTS() error

	// SetVolume sets speaker volume on HAL (0-100).
	SetVolume(pct int) error

	// StartHALVoice starts the voice pipeline on HAL. sttKey / ttsKey
	// and sttBaseURL / ttsBaseURL are the AutonomousSTT and TTS endpoints;
	// pass empty for any to fall back to llmKey / llmBaseURL.
	StartHALVoice(deepgramKey, llmKey, sttKey, ttsKey, llmBaseURL, sttBaseURL, ttsBaseURL, ttsVoice, ttsInstructions, ttsProvider string) error

	// WatchIdentity polls IDENTITY.md and pushes updated wake words to HAL on rename.
	WatchIdentity(ctx context.Context)

	// UpdateIdentityName rewrites the **Name:** line in workspace/IDENTITY.md.
	// WatchIdentity picks up the change within its next poll cycle and pushes the
	// new wake words to HAL.
	UpdateIdentityName(name string) error

	// StartSkillWatcher polls OTA metadata for skill version changes and notifies the agent.
	StartSkillWatcher(ctx context.Context)

	// StartModelSync periodically reconciles the upstream model list (ModelsAPIURL)
	// into openclaw.json. Fail-soft: a failed fetch logs and continues. Restarts
	// the gateway only when the file actually changed.
	StartModelSync(ctx context.Context)

	// UpdatePrimaryModel patches agents.defaults.model.primary in openclaw.json
	// to "autonomous/{modelKey}" and restarts the gateway. No-op when modelKey
	// is empty or when openclaw.json does not exist yet.
	UpdatePrimaryModel(modelKey string) error

	// StartPrimaryModelWatch watches the openclaw config directory for external
	// changes to openclaw.json. When a change is detected without an os-server write
	// flag, it reads the new primary model and syncs it to config.LLMModel
	// (only when provider == "autonomous"; others are silently ignored).
	StartPrimaryModelWatch(ctx context.Context)

	// GetConfiguredChannel returns the primary messaging channel type configured
	// in the agent runtime (e.g. "telegram", "discord", "slack").
	// Returns "channel" if none can be determined.
	GetConfiguredChannel() string

	// CompactSession sends a sessions.compact RPC to the agent runtime
	// to summarize and reduce conversation history for the given session.
	CompactSession(sessionKey string) error

	// NewSession sends a sessions.new RPC to start a fresh conversation
	// session for the given key. Unlike CompactSession (which runs a
	// summarize LLM call and can take 30-60s+), this is instant — the
	// runtime drops in-session history and starts clean. External device
	// memory (mood log, habit tracking, owner identity, voice clusters)
	// is unaffected because it lives outside the agent session JSONL.
	NewSession(sessionKey string) error

	// IsRecentOutboundChat returns true if the os-server just called chat.send with
	// this exact text within the recent window. Used by the session.message
	// handler to skip echoes of os-server-injected user messages (wake greeting,
	// ambient guard, sensing events) which OpenClaw broadcasts back as
	// session.message role=user — identical in shape to real channel input.
	IsRecentOutboundChat(text string) bool
}
