package http

import (
	"log/slog"
	"math/rand"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/internal/intent"
	"go.autonomous.ai/os/lib/i18n"
	"go.autonomous.ai/os/lib/hal"
)

// Dead air filler — short TTS cues spoken by HAL while OpenClaw is busy,
// scheduled and cancelled by FillerManager from agent lifecycle/tool events.
//
// Two pools chosen by turn position:
//   - OpeningFillers: first filler of a turn — acknowledges the user
//     just before the agent starts working.
//   - ContinuationFillers: re-arms after tool.end — implies progress
//     ("still working") rather than re-acknowledging.
//
// Pool empty = that position is silent. Both empty = feature disabled.

// poolsForLang returns the (opening, continuation) pools for a BCP-47 STT
// language code. Empty / unknown / "en*" → English. Falls through to English
// when the requested pool is empty so a misconfigured pool stays graceful.
func poolsForLang(lang string) (opening, continuation []string) {
	return i18n.FillerOpening(lang), i18n.FillerContinuation(lang)
}

// toolPoolForLang returns the tool-specific filler pool for (lang, toolName).
// Returns nil when there's no pool for that combination — caller falls back
// to the regular Continuation pool. Unknown lang → English pool.
func toolPoolForLang(lang, toolName string) []string {
	return i18n.FillerForTool(lang, toolName)
}

// Filler tuning. All durations are wall-clock.
const (
	// FillerDelay is how long to wait after the agent starts (or finishes
	// a non-reactive tool) before speaking a filler. If the assistant
	// reply or a hardware reaction arrives first the timer is cancelled
	// and no filler plays.
	FillerDelay = 1500 * time.Millisecond

	// FillerCooldown is the minimum gap between two filler reactions in
	// the same turn — covers both filler-spoken and hardware-reaction
	// events. Keeps the lamp from chattering "one sec... still working"
	// on top of "/emotion thinking" within a fraction of a second.
	// Tuned 2026-05-12 from 4s → 2.5s so short ~3s tool gaps still get a
	// filler instead of going silent — cached audio plays in ~1s so a
	// 2.5s cooldown leaves ~1.5s of dead air between fillers, enough to
	// not feel chattery while still covering more gaps.
	FillerCooldown = 2500 * time.Millisecond

	// MaxFillersPerTurn caps actual spoken fillers in a single turn.
	// Hardware reactions don't count against this — only TTS plays.
	// Bumped 2026-05-12 from 3 → 6 to cover multi-tool turns (4+ tool
	// boundaries observed on web_search + web_fetch chains) where every
	// gap should get a filler for best perceived progress UX. With pool
	// sizes ≥ 12 (post-2026-05-12), 5 Continuations + 1 synthetic Opening
	// stays varied enough to avoid feeling robotic.
	MaxFillersPerTurn = 6
)

// fillerCancelToolMarkers are URL fragments for tool calls that themselves
// act as audible/visible reactions — when one fires, no filler is needed
// at that moment because the user already perceived the lamp reacting.
var fillerCancelToolMarkers = []string{"/emotion", "/audio/play", "/scene", "/servo"}

// isHWReactionTool reports whether toolArgs invokes a hardware reaction.
func isHWReactionTool(toolArgs string) bool {
	if toolArgs == "" {
		return false
	}
	for _, m := range fillerCancelToolMarkers {
		if strings.Contains(toolArgs, m) {
			return true
		}
	}
	return false
}

// fillerRun is the per-turn state tracked by FillerManager.
type fillerRun struct {
	timer          *time.Timer
	playing        bool
	fired          int       // count of fillers actually spoken this turn
	lastActivityAt time.Time // last time something audible/visible happened (filler or HW tool)
	ended          bool      // turn finalized (assistant delta or lifecycle.end) — no more arms
	lastSpoken     string    // text of the most recent filler — used to dedup back-to-back picks
	rearmPending   bool      // tool.end arrived while playing=true; fire() re-arms after speak (otherwise the event would be silently dropped by armLocked's playing guard)
	lastToolName   string    // name of the most recently started tool — drives tool-aware filler pool ("Đang tra mạng" for web_search, "Đang đọc tài liệu" for read, etc.). Empty when no tool has started yet (e.g. first filler before any tool call)
}

// fillersDisabled reports whether both English pools are empty — the kill
// switch. Per-language pools are not considered: emptying English alone
// disables the feature for every language.
func fillersDisabled() bool {
	return len(i18n.FillerOpening(i18n.LangEN)) == 0 && len(i18n.FillerContinuation(i18n.LangEN)) == 0
}

// pickFiller returns a phrase appropriate for the current turn position
// in the active language (read from i18n.Lang()), avoiding lastSpoken when
// an alternative exists.
//
// Lookup chain (first non-empty wins):
//  1. Tool-specific pool keyed by lastToolName ("web_search" → "Đang tra mạng")
//  2. Opening pool when fired==0, else Continuation pool
//  3. The opposite (Continuation/Opening) pool as fallback
//
// fired==0 prefers Opening so the very first filler stays an
// acknowledgement; once a tool has fired the tool pool drives accuracy.
func pickFiller(fired int, lastSpoken, lastToolName string) string {
	lang := i18n.Lang()
	if pool := toolPoolForLang(lang, lastToolName); len(pool) > 0 {
		if pick := pickFrom(pool, lastSpoken); pick != "" {
			return pick
		}
	}
	opening, continuation := poolsForLang(lang)
	primary, fallback := opening, continuation
	if fired > 0 {
		primary, fallback = continuation, opening
	}
	if pick := pickFrom(primary, lastSpoken); pick != "" {
		return pick
	}
	return pickFrom(fallback, lastSpoken)
}

// classifyFillerPool reports which pool the given filler text came from,
// purely for debug logging on the fire path. Looks up the text in the
// tool pool, opening, and continuation lists (in that order) and returns
// the first hit. Returned values: "tool:<name>", "opening", "continuation",
// or "unknown" when the filler doesn't appear in any pool (shouldn't happen
// unless pools were edited at runtime between pick and classify).
func classifyFillerPool(filler, toolName string, fired int, lang string) string {
	if filler == "" {
		return "none"
	}
	if pool := toolPoolForLang(lang, toolName); poolContains(pool, filler) {
		return "tool:" + toolName
	}
	opening, continuation := poolsForLang(lang)
	if poolContains(opening, filler) {
		return "opening"
	}
	if poolContains(continuation, filler) {
		return "continuation"
	}
	return "unknown"
}

// poolContains is a tiny linear-scan helper; pools are at most ~12 entries
// so a map lookup isn't worth the allocation churn.
func poolContains(pool []string, s string) bool {
	for _, p := range pool {
		if p == s {
			return true
		}
	}
	return false
}

// pickFrom returns a random entry from pool. When the pool has more than
// one entry it avoids returning lastSpoken so the same line doesn't fire
// twice in a row within a turn.
func pickFrom(pool []string, lastSpoken string) string {
	switch len(pool) {
	case 0:
		return ""
	case 1:
		return pool[0]
	}
	pick := pool[rand.Intn(len(pool))]
	if pick == lastSpoken {
		// Re-roll once. With pool size >= 2, two picks bound collision
		// probability tightly enough — no need to loop.
		pick = pool[rand.Intn(len(pool))]
		if pick == lastSpoken {
			// Deterministic fallback: walk to the next index.
			for i, p := range pool {
				if p == lastSpoken {
					pick = pool[(i+1)%len(pool)]
					break
				}
			}
		}
	}
	return pick
}

// PrewarmFillers asks hal to render+save WAV for every filler phrase
// in the active STT language (read from i18n.Lang()) so the first runtime
// fire is a cache hit (no ElevenLabs roundtrip). Polls hal /health
// until it answers (os-server.service starts before hal.service is
// ready -- without this guard every prerender races and all phrases
// fail with connection refused). Then prerenders serially. Logs failures
// but never panics; cache misses fall back to live speak at fire time.
//
// "vi", "zh-CN", "zh-TW" pick the matching translated pool; anything
// else falls back to English. intent.CacheableReplies is always English
// (intent rules only match English keywords) so it's prewarmed regardless
// of language. Switching language at runtime causes a one-time miss on
// the first filler — acceptable since hal restarts on EditConfig
// anyway.
func PrewarmFillers() {
	lang := i18n.Lang()
	const (
		readyMaxWait  = 120 * time.Second
		readyInterval = 2 * time.Second
		perPhraseRetry = 3
	)
	deadline := time.Now().Add(readyMaxWait)
	ready := false
	for time.Now().Before(deadline) {
		if _, err := hal.GetHealth(); err == nil {
			ready = true
			break
		}
		time.Sleep(readyInterval)
	}
	if !ready {
		slog.Warn("filler prewarm aborted: hal /health not reachable", "component", "sensing")
		return
	}

	opening, continuation := poolsForLang(lang)
	all := append([]string{}, opening...)
	all = append(all, continuation...)
	// Also prerender tool-specific filler phrases. Without this, the first
	// fire of a tool-aware filler (e.g. "Đang tra mạng" when web_search
	// runs) hits ElevenLabs live (~1-2s render) and the resulting late
	// audio races against the assistant TTS that follows — user perceives
	// it as the filler getting cut off / TTS being suppressed.
	// Flatten every tool override across all langs so prerender covers the
	// pool for the currently-active lang (poolsForLang via i18n).
	for _, tool := range []string{
		"web_search", "x_search", "web_fetch", "read", "memory_search",
		"memory_get", "exec", "process", "image_generate", "video_generate",
		"music_generate", "update_plan", "session_status", "apply_patch",
		"pdf", "canvas", "nodes", "subagents", "image",
	} {
		all = append(all, i18n.FillerForTool(lang, tool)...)
	}
	all = append(all, intent.CacheableReplies...)
	// Dedup so overlapping phrases (e.g. between a tool pool and the
	// generic Continuation pool) only prerender once per language.
	seen := make(map[string]struct{}, len(all))
	unique := make([]string, 0, len(all))
	for _, p := range all {
		if _, ok := seen[p]; ok {
			continue
		}
		seen[p] = struct{}{}
		unique = append(unique, p)
	}
	all = unique
	rendered := 0
	for _, phrase := range all {
		var lastErr error
		for attempt := 1; attempt <= perPhraseRetry; attempt++ {
			if err := hal.PrerenderCached(phrase); err != nil {
				lastErr = err
				time.Sleep(time.Duration(attempt) * time.Second)
				continue
			}
			lastErr = nil
			break
		}
		if lastErr != nil {
			slog.Warn("filler prerender failed", "component", "sensing", "phrase", phrase, "error", lastErr)
			continue
		}
		rendered++
		slog.Debug("filler prerendered", "component", "sensing", "phrase", phrase)
	}
	slog.Info("filler cache prewarm complete", "component", "sensing", "lang", lang, "rendered", rendered, "total", len(all))
}

// PlayOpeningFillerNow fires a single Opening-pool filler immediately,
// fire-and-forget, without going through FillerManager. Called by the
// sensing handler right after a voice/voice_command turn is forwarded.
//
// Pool is picked from i18n.Lang() (see poolsForLang). Uses the hal WAV
// cache (SpeakCachedInterruptible) so the filler nhả tiếng ~50ms after this
// call instead of 1.5s — fillers were previously fired ~5-10s ahead of the
// real reply just to mask ElevenLabs latency; with cached audio that
// workaround is unnecessary, but the call site stays the same for now.
//
// No-op when the resolved Opening pool is empty.
func PlayOpeningFillerNow() {
	lang := i18n.Lang()
	opening, _ := poolsForLang(lang)
	if len(opening) == 0 {
		return
	}
	filler := pickFrom(opening, "")
	if filler == "" {
		return
	}
	slog.Info("opening filler firing (immediate, cached)", "component", "sensing", "lang", lang, "filler", filler)
	if err := hal.SpeakCachedInterruptible(filler); err != nil {
		slog.Warn("opening filler failed", "component", "sensing", "error", err)
	}
}

// FillerManager schedules and cancels dead-air fillers driven by OpenClaw
// agent events. Wiring (per turn lifecycle):
//
//   1. Sensing handler calls MarkVoiceRun(runID) before forwarding a
//      voice/voice_command turn — only marked runs are eligible.
//   2. SSE handler calls OnTurnStart(runID) on lifecycle.start — arms the
//      first FillerDelay timer.
//   3. SSE handler calls OnToolStart(runID, toolArgs) on tool.start —
//      hardware tools (/emotion, /audio/play, /scene, /servo) soft-cancel
//      the pending filler since the agent already reacted; non-hardware
//      tools (Bash, Read, etc.) leave the timer alone.
//   4. SSE handler calls OnToolEnd(runID) on tool.end — re-arms a filler
//      timer if the turn is still active and the cap/cooldown allow it.
//      This covers long multi-tool turns where each tool boundary is a
//      potential dead-air pocket.
//   5. SSE handler calls Cancel(runID) on the first assistant delta and
//      again on lifecycle.end — hard cancel: stops any pending timer,
//      interrupts a filler mid-speech via hal.StopTTS(), and clears
//      run state so further events are no-ops.
//
// All exported methods are safe for concurrent use and idempotent.
type FillerManager struct {
	mu        sync.Mutex
	runs      map[string]*fillerRun
	voiceRuns map[string]bool
}

// NewFillerManager constructs an empty FillerManager. Language is read at
// fire time from lib/i18n, so no config wiring is needed here.
func NewFillerManager() *FillerManager {
	return &FillerManager{
		runs:      make(map[string]*fillerRun),
		voiceRuns: make(map[string]bool),
	}
}

// DefaultFillerManager is the process-wide singleton shared by the sensing
// HTTP handler (MarkVoiceRun) and the OpenClaw SSE event handler
// (OnTurnStart/OnToolStart/OnToolEnd/Cancel).
var DefaultFillerManager = NewFillerManager()

// MarkVoiceRun marks runID as eligible for fillers. Other turn types
// (Telegram, web chat, passive sensing, cron, guard) must NOT be marked.
func (fm *FillerManager) MarkVoiceRun(runID string) {
	if runID == "" {
		return
	}
	fm.mu.Lock()
	fm.voiceRuns[runID] = true
	fm.mu.Unlock()
}

// OnTurnStart records the run as active and arms a Continuation timer so
// dead air gets filled even when the agent thinks without invoking any
// tool (no tool.end -> no OnToolEnd re-arm without this). fired=1 marks
// Opening as already played by the sensing handler so pickFiller prefers
// the Continuation pool here.
//
// The arm-on-turn-start path was previously disabled because ElevenLabs
// TTFB > 2s could exceed hal speak() lock-timeout=2s; with the WAV
// cache (2026-05-05), cached fillers play in ~50ms so the race is gone.
func (fm *FillerManager) OnTurnStart(runID string) {
	if runID == "" || fillersDisabled() {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	if !fm.voiceRuns[runID] {
		return
	}
	delete(fm.voiceRuns, runID)
	if _, exists := fm.runs[runID]; exists {
		return
	}
	run := &fillerRun{fired: 1, lastActivityAt: time.Now()}
	fm.runs[runID] = run
	fm.armLocked(runID, run, FillerDelay)
}

// OnToolStart records the most recently started tool name so the next
// filler picks a tool-aware phrase (see ToolFillers*), and soft-cancels
// the pending filler when the tool is a hardware reaction (the user
// already perceives the lamp reacting — no filler needed at that moment).
// Non-hardware tools leave the filler timer ticking so it can still fire
// during a long Bash/Read/web_search.
func (fm *FillerManager) OnToolStart(runID, toolArgs, toolName string) {
	if runID == "" {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	run, ok := fm.runs[runID]
	if !ok || run.ended {
		slog.Debug("filler OnToolStart skipped — no active run", "component", "sensing", "run_id", runID, "tool", toolName)
		return
	}
	if toolName != "" {
		run.lastToolName = toolName
	}
	hw := isHWReactionTool(toolArgs)
	slog.Info("filler OnToolStart", "component", "sensing", "run_id", runID, "tool", toolName, "hw", hw, "fired", run.fired, "playing", run.playing, "timer_armed", run.timer != nil)
	if !hw {
		return
	}
	fm.softCancelLocked(run)
}

// OnToolEnd attempts to re-arm a filler timer after a tool finishes —
// the turn may still have minutes of thinking ahead. No-op when the run
// has ended or the per-turn cap is reached. When a filler is currently
// speaking, the arm is deferred via run.rearmPending so fire() can
// schedule the next timer once speech completes — without this defer
// the tool.end is silently dropped (armLocked refuses while playing)
// and the next dead-air gap goes unfilled.
func (fm *FillerManager) OnToolEnd(runID string) {
	if runID == "" || fillersDisabled() {
		return
	}
	fm.mu.Lock()
	defer fm.mu.Unlock()
	run, ok := fm.runs[runID]
	if !ok || run.ended {
		slog.Debug("filler OnToolEnd skipped — no active run", "component", "sensing", "run_id", runID)
		return
	}
	if run.playing {
		run.rearmPending = true
		slog.Info("filler OnToolEnd deferred (playing) — rearm after speak", "component", "sensing", "run_id", runID, "tool", run.lastToolName, "fired", run.fired)
		return
	}
	delay := FillerDelay
	if !run.lastActivityAt.IsZero() {
		// Respect cooldown from the last filler/HW reaction. Add the
		// regular delay on top so we don't immediately re-fire the moment
		// cooldown elapses — give the next thought a chance.
		if elapsed := time.Since(run.lastActivityAt); elapsed < FillerCooldown {
			delay = (FillerCooldown - elapsed) + FillerDelay
		}
	}
	slog.Info("filler OnToolEnd arming", "component", "sensing", "run_id", runID, "tool", run.lastToolName, "fired", run.fired, "delay_ms", delay.Milliseconds())
	fm.armLocked(runID, run, delay)
}

// Cancel hard-cancels the run: stop pending timer, interrupt any filler
// mid-speech, mark the run ended so future tool events are no-ops, and
// drop the entry from the runs map. Idempotent.
func (fm *FillerManager) Cancel(runID string) {
	if runID == "" {
		return
	}
	fm.mu.Lock()
	delete(fm.voiceRuns, runID)
	run, ok := fm.runs[runID]
	if !ok {
		fm.mu.Unlock()
		return
	}
	run.ended = true
	if run.timer != nil {
		run.timer.Stop()
		run.timer = nil
	}
	wasPlaying := run.playing
	run.playing = false
	delete(fm.runs, runID)
	fm.mu.Unlock()

	if wasPlaying {
		go func() {
			if err := hal.StopTTS(); err != nil {
				slog.Warn("filler stop TTS failed", "component", "sensing", "run_id", runID, "error", err)
			}
		}()
	}
}

// armLocked schedules a filler timer for run after delay. Caller holds fm.mu.
// No-op when the run has ended, the cap is reached, or a timer/filler is already active.
func (fm *FillerManager) armLocked(runID string, run *fillerRun, delay time.Duration) {
	if run.ended {
		slog.Debug("filler arm blocked — ended", "component", "sensing", "run_id", runID)
		return
	}
	if run.fired >= MaxFillersPerTurn {
		slog.Info("filler arm blocked — cap reached", "component", "sensing", "run_id", runID, "fired", run.fired, "cap", MaxFillersPerTurn)
		return
	}
	if run.timer != nil {
		slog.Debug("filler arm blocked — timer already pending", "component", "sensing", "run_id", runID)
		return
	}
	if run.playing {
		slog.Debug("filler arm blocked — currently playing", "component", "sensing", "run_id", runID)
		return
	}
	run.timer = time.AfterFunc(delay, func() { fm.fire(runID) })
}

// softCancelLocked clears a pending timer and interrupts in-flight TTS,
// but keeps the run alive so OnToolEnd can re-arm later. Counts as an
// activity so the cooldown applies to the next re-arm.
func (fm *FillerManager) softCancelLocked(run *fillerRun) {
	if run.timer != nil {
		run.timer.Stop()
		run.timer = nil
	}
	wasPlaying := run.playing
	run.playing = false
	run.lastActivityAt = time.Now()
	if wasPlaying {
		go func() {
			if err := hal.StopTTS(); err != nil {
				slog.Warn("filler stop TTS failed (soft cancel)", "component", "sensing", "error", err)
			}
		}()
	}
}

// fire is the timer callback. Re-checks state under the lock, picks a
// pool-appropriate filler, speaks it outside the lock, then re-takes the
// lock to update counters.
func (fm *FillerManager) fire(runID string) {
	fm.mu.Lock()
	run, ok := fm.runs[runID]
	if !ok || run.ended || run.timer == nil {
		// Cancel raced ahead between AfterFunc firing and this callback.
		fm.mu.Unlock()
		return
	}
	filler := pickFiller(run.fired, run.lastSpoken, run.lastToolName)
	if filler == "" {
		// Both pools empty after live edit. Bail without playing.
		run.timer = nil
		fm.mu.Unlock()
		slog.Warn("filler fire bail — both pools empty", "component", "sensing", "run_id", runID, "tool", run.lastToolName)
		return
	}
	run.timer = nil
	run.playing = true
	toolName := run.lastToolName
	fired := run.fired
	fm.mu.Unlock()

	// Show whether the picked filler came from a tool-specific pool (matches
	// what the agent is doing right now) or fell back to the generic
	// Continuation pool (tool name unmapped). Speeds up "I didn't hear a
	// filler for web_search" debugging — grep run_id, see pool=tool vs
	// pool=continuation vs pool=opening at fire time.
	pool := classifyFillerPool(filler, toolName, fired, i18n.Lang())
	slog.Info("dead air filler firing", "component", "sensing", "run_id", runID, "filler", filler, "tool", toolName, "fired", fired, "pool", pool)
	if err := hal.SpeakCachedInterruptible(filler); err != nil {
		slog.Warn("dead air filler failed", "component", "sensing", "run_id", runID, "error", err)
	}

	fm.mu.Lock()
	// Cancel may have run during SpeakInterruptible — in that case the
	// run was deleted; nothing to update.
	if run, ok := fm.runs[runID]; ok && !run.ended {
		run.playing = false
		run.fired++
		run.lastActivityAt = time.Now()
		run.lastSpoken = filler
		// Pick up tool.end events that landed during speech (run.playing
		// was true so OnToolEnd deferred them via rearmPending). Use the
		// plain FillerDelay — cooldown is implicit since speech already
		// took ~1s of wall-clock, so total gap to next fire is ~speak +
		// FillerDelay ≈ FillerCooldown.
		if run.rearmPending {
			run.rearmPending = false
			fm.armLocked(runID, run, FillerDelay)
		}
	}
	fm.mu.Unlock()
}
