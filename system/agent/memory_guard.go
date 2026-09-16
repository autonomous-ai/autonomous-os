package agent

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/config"
)

const (
	// memoryGuardDebounce coalesces the burst of events one agent write
	// produces (truncate + several writes, or temp + rename) into one pass.
	memoryGuardDebounce = 2 * time.Second
	// memoryGuardRescan re-adds watch dirs that did not exist at boot — a
	// runtime installed by a later switch — and re-sweeps as a safety net.
	memoryGuardRescan = 10 * time.Minute
)

// MemoryGuard keeps self-written agent memory from steering routing (#421).
//
// It runs migratepersona.GuardMemoryFiles once at boot (after persona
// migration, so a freshly migrated poison is caught in the same boot) and then
// on every write to any runtime's USER.md / MEMORY.md, seconds after the agent
// writes — before the next OpenClaw turn re-reads the bootstrap files and
// before the next Hermes session loads memories/. Every runtime is watched,
// not just the active one: persona files are copies and an untouched copy
// migrates back on the next switch.
type MemoryGuard struct {
	opts    migratepersona.Options
	execute bool
	runtime migratepersona.Runtime // active runtime, whose files make up the published state

	mu sync.Mutex
	// lastWritten is sha8(content) of what the guard itself last wrote to each
	// path. The rename that lands our rewrite is itself a watch event; matching
	// the hash tells it apart from an agent write and stops a rewrite loop.
	lastWritten map[string]string

	// sweepMu serialises whole sweeps so a timer-driven onChange and a rescan
	// Run cannot guard the same file twice: both would read the poisoned file,
	// both miss lastWritten, and both back up, quarantine and report it. g.mu
	// only protects the map, not the read-compare-guard sequence.
	sweepMu sync.Mutex
}

// ProvideMemoryGuard builds the guard from device config (Wire).
func ProvideMemoryGuard(cfg *config.Config) *MemoryGuard {
	current, _, _ := resolveRuntime(cfg)
	return &MemoryGuard{
		opts:        migratepersona.DefaultOptions(cfg.OpenclawConfigDir, hermesHome),
		execute:     cfg.MemoryGuardEnabled(),
		runtime:     migratepersona.Runtime(current),
		lastWritten: map[string]string{},
	}
}

// Run is one sweep of every runtime's USER.md + MEMORY.md. Logs and never
// blocks startup; a failure here must never keep the device from booting.
func (g *MemoryGuard) Run(trigger string) {
	g.sweepMu.Lock()
	defer g.sweepMu.Unlock()
	actions, err := migratepersona.GuardMemoryFiles(g.opts, g.execute)
	if err != nil {
		slog.Warn("memory guard sweep failed; files left untouched",
			"component", "memory-guard", "trigger", trigger, "error", err)
	}
	for _, a := range actions {
		g.report(a.Path, &a, trigger)
	}
	g.publishState()
}

// Watch blocks until ctx is done, sweeping a file whenever it changes.
func (g *MemoryGuard) Watch(ctx context.Context) {
	targets := map[string]bool{}
	for _, p := range append(migratepersona.UserProfilePaths(g.opts), migratepersona.MemoryFilePaths(g.opts)...) {
		targets[p] = true
	}
	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		slog.Error("memory guard: create watcher failed", "component", "memory-guard", "error", err)
		return
	}
	defer watcher.Close()

	watched := map[string]bool{}
	addDirs := func() {
		for p := range targets {
			dir := filepath.Dir(p)
			if watched[dir] {
				continue
			}
			if err := watcher.Add(dir); err == nil {
				watched[dir] = true
			} // a runtime that is not installed has no dir yet; the rescan retries
		}
	}
	addDirs()
	slog.Info("memory guard watching", "component", "memory-guard", "dirs", len(watched), "execute", g.execute)

	var tmu sync.Mutex
	timers := map[string]*time.Timer{}
	schedule := func(path string) {
		tmu.Lock()
		defer tmu.Unlock()
		if t := timers[path]; t != nil {
			t.Stop()
		}
		timers[path] = time.AfterFunc(memoryGuardDebounce, func() { g.onChange(path, "watch") })
	}

	rescan := time.NewTicker(memoryGuardRescan)
	defer rescan.Stop()
	for {
		select {
		case <-ctx.Done():
			tmu.Lock()
			for _, t := range timers {
				t.Stop()
			}
			tmu.Unlock()
			return
		case ev, ok := <-watcher.Events:
			if !ok {
				return
			}
			if targets[ev.Name] && (ev.Has(fsnotify.Write) || ev.Has(fsnotify.Create) || ev.Has(fsnotify.Rename)) {
				schedule(ev.Name)
			}
		case err, ok := <-watcher.Errors:
			if !ok {
				return
			}
			slog.Warn("memory guard watcher error", "component", "memory-guard", "error", err)
		case <-rescan.C:
			addDirs()
			g.Run("rescan")
		}
	}
}

// onChange is the debounced per-file handler.
func (g *MemoryGuard) onChange(path, trigger string) {
	g.sweepMu.Lock()
	defer g.sweepMu.Unlock()
	raw, err := os.ReadFile(path)
	if err != nil {
		return // removed between event and timer; nothing to guard
	}
	sum := migratepersona.Sha8(raw)
	g.mu.Lock()
	own := g.lastWritten[path] == sum
	g.mu.Unlock()
	if own {
		return
	}
	act, err := migratepersona.GuardMemoryFile(path, migratepersona.EnrolledLabels(), g.execute)
	if err != nil {
		slog.Warn("memory guard failed on change; file left untouched",
			"component", "memory-guard", "path", path, "error", err)
	}
	g.report(path, act, trigger)
	g.publishState()
}

// report records our own write (if any), logs and emits the memory_changed
// flow event. Emitted for EVERY observed change, clean or not: the point is to
// let Flow Monitor tie a routing regression to a memory write. No content
// leaves this function — sizes, hashes, reasons and counts only.
func (g *MemoryGuard) report(path string, act *migratepersona.GuardAction, trigger string) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	sum := migratepersona.Sha8(raw)
	if act != nil && act.Written {
		g.mu.Lock()
		g.lastWritten[path] = sum
		g.mu.Unlock()
	}
	quarantined := 0
	var reasons []string
	if act != nil {
		quarantined = len(act.Dropped)
		for _, d := range act.Dropped {
			reasons = append(reasons, d.Reason)
		}
	}
	data := map[string]any{
		"file":        filepath.Base(path),
		"runtime":     migratepersona.RuntimeOfPath(g.opts, path),
		"path":        path,
		"size":        len(raw),
		"sha8":        sum,
		"quarantined": quarantined,
		"reasons":     reasons,
		"execute":     g.execute,
		"trigger":     trigger,
	}
	flow.Log("memory_changed", data)
	if quarantined == 0 {
		slog.Info("memory changed", "component", "memory-guard", "path", path, "size", len(raw), "sha8", sum, "trigger", trigger)
		return
	}
	mode := "quarantined"
	if !g.execute {
		mode = "would quarantine (agent.memory_guard=false)"
	}
	slog.Warn("memory guard "+mode, "component", "memory-guard", "path", path,
		"blocks", quarantined, "reasons", reasons, "sidecar", migratepersona.QuarantinePath(path), "trigger", trigger)
}

func (g *MemoryGuard) publishState() {
	migratepersona.SetMemoryState(migratepersona.ReadMemoryState(g.runtime, g.opts))
}
