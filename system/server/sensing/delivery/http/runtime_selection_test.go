package http

import (
	"net/http"
	"testing"

	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

type selectedSensingGateway struct {
	busyGateway
	runtime string
}

func (g *selectedSensingGateway) Name() string { return g.runtime }

func TestRuntimeSelectionPreservesActiveSensing(t *testing.T) {
	t.Chdir(t.TempDir())
	for _, active := range []string{"hermes", "intern"} {
		cfg := &config.Config{AgentRuntime: active}
		if err := cfg.Save(); err != nil {
			t.Fatal(err)
		}
		gw := &selectedSensingGateway{runtime: active}
		h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: cfg}
		fired := 0
		h.SetOnRealtimeHandled(func() bool { fired++; return true })
		for _, selected := range []string{"intern", "hermes"} {
			if err := cfg.SelectAgentRuntime(selected); err != nil {
				t.Fatal(err)
			}
			rec := postRealtimeHandled(t, h)
			want := http.StatusOK
			if active == "intern" {
				want = http.StatusNotImplemented
			}
			if rec.Code != want {
				t.Fatalf("active=%s selected=%s status=%d body=%s", active, selected, rec.Code, rec.Body.String())
			}
		}
		want := 2
		if active == "intern" {
			want = 0
		}
		if gw.queued != want || fired != want {
			t.Fatalf("active=%s queued=%d hook=%d want=%d", active, gw.queued, fired, want)
		}
	}
}
