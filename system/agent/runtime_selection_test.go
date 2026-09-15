package agent

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type reconciliationGateway struct {
	domain.AgentGateway
	runtime string
}

func (g *reconciliationGateway) Name() string { return g.runtime }

func TestRuntimeSelectionReconciliationUsesActiveGateway(t *testing.T) {
	t.Chdir(t.TempDir())
	for _, active := range []string{"hermes", "intern"} {
		for _, baseline := range []string{"", "hermes"} {
			cfg := &config.Config{AgentRuntime: "hermes", ChannelsAppliedRuntime: baseline, MCPAppliedRuntime: baseline, LLMConfigAppliedRuntime: baseline}
			if err := cfg.Save(); err != nil {
				t.Fatal(err)
			}
			gw := &reconciliationGateway{runtime: active}
			// No effect methods are implemented: any unintended migration panics.
			for _, selected := range []string{"intern", "hermes"} {
				if err := cfg.SelectAgentRuntime(selected); err != nil {
					t.Fatal(err)
				}
				(&ChannelReconcile{cfg: cfg, gw: gw}).Reconcile()
				(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()
				(&ConfigMigration{cfg: cfg, gw: gw}).Reconcile()
				want := baseline
				if active == "hermes" {
					want = active
				}
				if cfg.ChannelsAppliedRuntime != want || cfg.MCPAppliedRuntime != want || cfg.LLMConfigAppliedRuntime != want {
					t.Fatalf("active=%s selected=%s: markers=%s/%s/%s want=%s", active, selected, cfg.ChannelsAppliedRuntime, cfg.MCPAppliedRuntime, cfg.LLMConfigAppliedRuntime, want)
				}
			}
		}
	}
}

func TestRuntimeSelectionConcurrentReconciliation(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := &config.Config{AgentRuntime: "hermes", ChannelsAppliedRuntime: "hermes", MCPAppliedRuntime: "hermes", LLMConfigAppliedRuntime: "hermes"}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	gw := &reconciliationGateway{runtime: "hermes"}
	done := make(chan error, 1)
	go func() {
		for i := 0; i < 50; i++ {
			if err := cfg.SelectAgentRuntime([]string{"intern", "hermes"}[i%2]); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	for i := 0; i < 100; i++ {
		(&ChannelReconcile{cfg: cfg, gw: gw}).Reconcile()
		(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()
		(&ConfigMigration{cfg: cfg, gw: gw}).Reconcile()
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if cfg.ChannelsAppliedRuntime != "hermes" || cfg.MCPAppliedRuntime != "hermes" || cfg.LLMConfigAppliedRuntime != "hermes" {
		t.Fatal("concurrent selection changed active reconciliation markers")
	}
}
