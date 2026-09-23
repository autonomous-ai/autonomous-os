package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type harnessShadowResolver interface {
	Resolve(context.Context, string, []jev.Candidate, jev.Options) jev.Selection
}

type harnessShadow struct {
	mu                sync.Mutex
	candidates        []jev.Candidate
	agentIDs          []string
	machine, instance string
	captured          time.Time
	busy              atomic.Bool
	resolver          harnessShadowResolver
	// Tests can observe outcomes without recording private provider payloads.
	report func(string, string, string, int64)
}

func newHarnessShadow() *harnessShadow {
	return &harnessShadow{resolver: jev.NewHarnessResolver(), report: func(outcome, actual, proposed string, elapsed int64) {
		slog.Info("Harness Jev shadow", "component", "harness", "outcome", outcome,
			"actual_agent_id", actual, "proposed_agent_id", proposed, "decision_ms", elapsed)
	}}
}

// Only reuse discovery already requested by the skill. Never truncate the agent
// set: excluding a candidate would make comparison misleading.
func (s *harnessShadow) remember(result harness.Frame, err error, status harness.Status) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.candidates, s.agentIDs = nil, nil
	if err != nil || result["error"] != nil || !status.Connected || status.MachineID == "" || status.ServerInstanceID == "" || result["machineId"] != status.MachineID {
		return
	}
	agents, ok := result["agents"].([]any)
	if !ok || len(agents) == 0 || len(agents) > 32 {
		return
	}
	candidates := make([]jev.Candidate, 0, len(agents))
	ids := make([]string, 0, len(agents))
	seen := map[string]bool{}
	for i, raw := range agents {
		agent, ok := raw.(map[string]any)
		if !ok {
			return
		}
		id, _ := agent["agentId"].(string)
		if id == "" || len(id) > 128 || seen[id] {
			return
		}
		seen[id] = true
		facts := map[string]any{}
		for _, key := range []string{"name", "recap", "workspace", "packageId", "runtime", "state", "engine"} {
			if value, exists := agent[key]; exists {
				facts[key] = value
			}
		}
		description, err := json.Marshal(facts)
		if err != nil || len(description) > 1000 {
			return
		}
		candidates = append(candidates, jev.Candidate{ID: fmt.Sprintf("session_%d", i), Description: string(description)})
		ids = append(ids, id)
	}
	s.candidates, s.agentIDs = candidates, ids
	s.machine, s.instance, s.captured = status.MachineID, status.ServerInstanceID, time.Now()
}

func (s *harnessShadow) compare(ctx context.Context, frame harness.Frame, status harness.Status, settings config.JevIntentSettings) {
	if s == nil || !settings.Enabled {
		return
	}
	actual, _ := frame["agentId"].(string)
	skip := func(reason string) { s.report("skip_"+reason, actual, "", 0) }
	if settings.Endpoint == "" || settings.APIKey == "" {
		skip("missing_config")
		return
	}
	text, _ := frame["text"].(string)
	if strings.TrimSpace(text) == "" || len(text) > 2000 || actual == "" {
		skip("input")
		return
	}
	s.mu.Lock()
	valid := status.Connected && frame["machineId"] == status.MachineID && s.machine == status.MachineID && s.instance == status.ServerInstanceID && time.Since(s.captured) <= 30*time.Second
	candidates := append([]jev.Candidate(nil), s.candidates...)
	ids := append([]string(nil), s.agentIDs...)
	s.mu.Unlock()
	found := false
	for _, id := range ids {
		found = found || id == actual
	}
	if !valid || !found {
		skip("snapshot")
		return
	}
	if ctx.Err() != nil {
		skip("cancelled")
		return
	}
	if !s.busy.CompareAndSwap(false, true) {
		skip("busy")
		return
	}
	go func() {
		defer s.busy.Store(false)
		budget := time.Duration(settings.TimeoutMS) * time.Millisecond
		if budget <= 0 || budget > 3*time.Second {
			budget = 3 * time.Second
		}
		worker, cancel := context.WithTimeout(ctx, budget)
		defer cancel()
		started := time.Now()
		choice := s.resolver.Resolve(worker, text, candidates, jev.Options{Enabled: true, Endpoint: settings.Endpoint, APIKey: settings.APIKey, Timeout: budget})
		proposed := ""
		if worker.Err() == nil {
			for i, candidate := range candidates {
				if choice.Intent == candidate.ID {
					proposed = ids[i]
					break
				}
			}
		}
		outcome := "abstain"
		if proposed != "" {
			outcome = "disagree"
			if proposed == actual {
				outcome = "agree"
			}
		}
		s.report(outcome, actual, proposed, time.Since(started).Milliseconds())
	}()
}
