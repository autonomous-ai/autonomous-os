package environment

import (
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// Exercise the same software contract with separate and integrated sensors.
// Unsupported metrics remain null, including while no component is ready.
func componentContractSnapshot(provider string, second int, temperature, co2 float64) Snapshot {
	s := Snapshot{
		State: "ready", Enabled: true, AgeS: number(0),
		Sample: map[string]*float64{}, Components: map[string]Snapshot{},
		Sources: map[string]string{}, MetricTimestamps: map[string]float64{},
	}
	for key := range config.DefaultEnvironmentConfig().Metrics {
		s.Sample[key] = nil
	}
	stamp := float64(1000 + second)
	s.Sample["timestamp"] = number(stamp)
	for key, value := range map[string]float64{"temperature_c": temperature, "co2_ppm": co2} {
		owner := provider
		if provider == "pair" {
			owner = "sen55"
			if key == "co2_ppm" {
				owner = "scd41"
			}
		}
		component, ok := s.Components[owner]
		if !ok {
			component = Snapshot{State: "ready", Enabled: true, AgeS: number(0), ContinuousDataS: number(100), Sample: map[string]*float64{"timestamp": number(stamp)}}
		}
		component.Sample[key] = number(value)
		s.Components[owner] = component
		s.Sample[key], s.Sources[key], s.MetricTimestamps[key] = number(value), owner, stamp
	}
	return s
}

func TestComponentReplacementUsesSameInitialAndChangeFlow(t *testing.T) {
	for _, provider := range []string{"pair", "sen63c", "future_component"} {
		t.Run(provider, func(t *testing.T) {
			cfg := config.DefaultEnvironmentConfig()
			cfg.SustainS, cfg.CooldownS, cfg.RetryIntervalS = 10, 1, 1
			d := NewDetector(cfg)
			s := componentContractSnapshot(provider, 0, 25, 700)
			mustNoEvent(t, d.Observe(epoch, s))
			initial, expires := d.startupSnapshot(epoch, s)
			if initial == nil || initial.Reason != "initial" || len(initial.Changes) != 0 || len(initial.Sample) != 3 {
				t.Fatalf("invalid initial event: %+v", initial)
			}
			startup := NewStartupCoordinator()
			startup.update(true, initial, expires)
			startup.FinishGreeting(true)
			pending, event := startup.initial(epoch, cfg.RetryIntervalS)
			if !pending || event == nil || event.Sample["voc_index"] != nil || event.Sample["nox_index"] != nil {
				t.Fatal("missing metrics blocked or polluted initial report")
			}
			d.Attempted(epoch, event, true)
			startup.acceptedInitial()
			mustNoEvent(t, d.Observe(epoch.Add(10*time.Second), componentContractSnapshot(provider, 10, 28, 1000)))
			event = d.Observe(epoch.Add(20*time.Second), componentContractSnapshot(provider, 20, 28, 1000))
			if event == nil || len(event.Changes) != 2 || event.Changes["temperature_c"].Delta != 3 || event.Changes["co2_ppm"].Delta != 300 {
				t.Fatalf("same metrics did not produce same changes: %+v", event)
			}
			for key, change := range event.Changes {
				if change.Source != s.Sources[key] {
					t.Fatalf("lost source for %s: %+v", key, change)
				}
			}
			if pending, _ := startup.initial(epoch.Add(20*time.Second), cfg.RetryIntervalS); pending {
				t.Fatal("repeated initial report")
			}
		})
	}
}

func TestAllNullContractIsUnavailable(t *testing.T) {
	s := componentContractSnapshot("sen63c", 0, 25, 700)
	s.State, s.Stale = "starting", true
	for key := range s.Sample {
		s.Sample[key] = nil
	}
	d := NewDetector(config.DefaultEnvironmentConfig())
	mustNoEvent(t, d.Observe(epoch, s))
	if initial, _ := d.startupSnapshot(epoch, s); initial != nil {
		t.Fatal("all-null sample must not create a startup report")
	}
}
