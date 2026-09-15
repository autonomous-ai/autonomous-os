package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestSelectAgentRuntimeChangesOnlySelection(t *testing.T) {
	dir := t.TempDir()
	originalPath := configPath
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = originalPath }()
	original := []byte(`{"agent_runtime":"openclaw","agent_remote_url":"http://must-stay","telegram_bot_token":"must-stay","unknown":{"nested":true}}`)
	if err := os.WriteFile(configPath, original, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{AgentRuntime: "openclaw"}
	if err := cfg.SelectAgentRuntime("intern"); err != nil {
		t.Fatal(err)
	}
	if cfg.AgentRuntime != "intern" {
		t.Fatalf("in-memory selection=%q", cfg.AgentRuntime)
	}
	var got map[string]json.RawMessage
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	var unknown map[string]bool
	if err := json.Unmarshal(got["unknown"], &unknown); err != nil {
		t.Fatal(err)
	}
	if string(got["agent_runtime"]) != `"intern"` || string(got["agent_remote_url"]) != `"http://must-stay"` || string(got["telegram_bot_token"]) != `"must-stay"` || !unknown["nested"] || len(unknown) != 1 {
		t.Fatalf("selection rewrite changed unrelated fields: %s", raw)
	}
}

func TestRuntimeSelectionConcurrentReads(t *testing.T) {
	originalPath := configPath
	configPath = filepath.Join(t.TempDir(), "config.json")
	defer func() { configPath = originalPath }()
	if err := os.WriteFile(configPath, []byte(`{"agent_runtime":"openclaw","unknown":true}`), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{AgentRuntime: "openclaw"}
	start := make(chan struct{})
	var wg sync.WaitGroup
	for reader := 0; reader < 4; reader++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			for i := 0; i < 1000; i++ {
				status := cfg.RuntimeSelectionStatus("openclaw", true)
				selected := status["selected"]
				if selected != "openclaw" && selected != "intern" {
					t.Errorf("unexpected selection: %v", selected)
				}
				if status["active"] != "openclaw" || status["current"] != "openclaw" || status["readiness_runtime"] != "openclaw" || status["ready"] != true || status["restart_required"] != (selected == "intern") {
					t.Errorf("inconsistent status: %v", status)
				}
			}
		}()
	}
	close(start)
	for i := 0; i < 50; i++ {
		runtime := "intern"
		if i%2 != 0 {
			runtime = "openclaw"
		}
		if err := cfg.SelectAgentRuntime(runtime); err != nil {
			t.Error(err)
			break
		}
	}
	wg.Wait()
}

func TestRuntimeSelectionPersistenceFailure(t *testing.T) {
	originalPath := configPath
	configPath = filepath.Join(t.TempDir(), "missing", "config.json")
	defer func() { configPath = originalPath }()
	cfg := &Config{AgentRuntime: "openclaw"}
	if err := cfg.SelectAgentRuntime("intern"); err == nil {
		t.Fatal("missing config unexpectedly saved")
	}
	if got := cfg.AgentRuntimeValue(); got != "openclaw" {
		t.Fatalf("failed selection changed memory: %q", got)
	}
}
