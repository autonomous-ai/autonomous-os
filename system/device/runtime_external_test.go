package device

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type activeRuntimeGateway struct {
	fakeGateway
	runtime string
	models  []string
}

func (g *activeRuntimeGateway) Name() string { return g.runtime }
func (g *activeRuntimeGateway) UpdatePrimaryModel(model string) error {
	g.models = append(g.models, model)
	return nil
}

func TestCurrentAgentRuntimeNilConfigFallback(t *testing.T) {
	if got := (&Service{}).CurrentAgentRuntime(); got != domain.AgentRuntimeOpenClaw {
		t.Fatalf("uninitialized runtime=%q, want openclaw", got)
	}
	s := newService(&activeRuntimeGateway{runtime: "hermes"}, nil)
	if got := s.CurrentAgentRuntime(); got != "hermes" {
		t.Fatalf("active runtime with nil config=%q, want hermes", got)
	}
}

func TestRuntimeSelectionPreservesActiveChannelAndConfig(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := &config.Config{AgentRuntime: "hermes"}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	gw := &activeRuntimeGateway{runtime: "hermes", fakeGateway: fakeGateway{supported: []string{domain.ChannelSlack}}}
	s := newService(gw, cfg)
	for _, selected := range []string{"intern", "hermes"} {
		if err := s.SelectExternalRuntime(domain.AgentRuntimeSetData{Runtime: selected}); err != nil {
			t.Fatal(err)
		}
		if s.externalRuntime() || s.CurrentAgentRuntime() != "hermes" || cfg.AgentRuntimeValue() != selected {
			t.Fatal("saved selection changed active gateway")
		}
		if _, err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "test-token", SlackAppToken: "test-app"}); err != nil {
			t.Fatal(err)
		}
		if _, err := s.RefreshChannelConfig(context.Background(), domain.ChannelSlack); err != nil {
			t.Fatal(err)
		}
		if err := s.UpdateConfig(domain.UpdateConfigRequest{LLMModel: "model-" + selected}); err != nil {
			t.Fatal(err)
		}
		if cfg.AgentRuntimeValue() != selected {
			t.Fatal("config update overwrote pending selection")
		}
	}
	if len(gw.addCalls) != 2 || len(gw.refreshCalls) != 2 || len(gw.models) != 2 {
		t.Fatalf("active effects: add=%d refresh=%d model=%v", len(gw.addCalls), len(gw.refreshCalls), gw.models)
	}
}

func TestActiveInternRejectsDeviceEffectsEvenAfterSelectingLegacy(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := &config.Config{AgentRuntime: "intern"}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	s := newService(&activeRuntimeGateway{runtime: "intern"}, cfg)
	for _, selected := range []string{"intern", "hermes"} {
		if err := s.SelectExternalRuntime(domain.AgentRuntimeSetData{Runtime: selected}); err != nil {
			t.Fatal(err)
		}
		for name, effect := range map[string]func() error{
			"config":     func() error { return s.UpdateConfig(domain.UpdateConfigRequest{LLMModel: "blocked"}) },
			"voice":      func() error { return s.UpdateVoiceConfig("", "", "", nil) },
			"realtime":   func() error { return s.UpdateRealtimeConfig(domain.RealtimeSetData{}) },
			"defaults":   func() error { return s.RestoreAutonomousDefaults("llm") },
			"mcp add":    func() error { return s.AddMCPTool(config.MCPTool{}) },
			"mcp remove": func() error { return s.RemoveMCPTool("test") },
			"setup":      func() error { return s.Setup(domain.SetupRequest{}) },
			"wifi":       func() error { return s.ReprovisionWifi(domain.WifiProvisionRequest{}) },
		} {
			if err := effect(); !errors.Is(err, domain.ErrNotSupportedByRuntime) {
				t.Errorf("%s with selection %s: %v", name, selected, err)
			}
		}
		if cfg.LLMModel != "" {
			t.Fatal("restricted request mutated config")
		}
	}
}

func TestRuntimeSelectionConcurrentConfigEffects(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := &config.Config{AgentRuntime: "hermes"}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	gw := &activeRuntimeGateway{runtime: "hermes"}
	s := newService(gw, cfg)
	done := make(chan error, 1)
	go func() {
		for i := 0; i < 50; i++ {
			if err := s.SelectExternalRuntime(domain.AgentRuntimeSetData{Runtime: []string{"intern", "hermes"}[i%2]}); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	for i := 0; i < 50; i++ {
		if err := s.UpdateConfig(domain.UpdateConfigRequest{LLMModel: []string{"model-a", "model-b"}[i%2]}); err != nil {
			t.Errorf("active config effect: %v", err)
		}
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if len(gw.models) != 50 {
		t.Fatalf("active model effects=%d, want 50", len(gw.models))
	}
}
