package environment

import (
	"math"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

func readyStartup(t *testing.T) (*StartupCoordinator, *Detector, *Event) {
	t.Helper()
	d := NewDetector(testSettings())
	s := sample(0, 24)
	s.ContinuousDataS = number(100)
	d.Observe(epoch, s)
	event, expires := d.startupSnapshot(epoch, s)
	if event == nil {
		t.Fatal("warm HAL snapshot missing")
	}
	c := NewStartupCoordinator()
	c.update(true, event, expires)
	return c, d, event
}

func TestStartupGreetingReservesWithoutDuplicate(t *testing.T) {
	c, d, _ := readyStartup(t)
	greeting := c.ReserveGreeting(epoch)
	if greeting == nil || greeting.Reason != "initial" || len(greeting.Changes) != 0 {
		t.Fatal("missing non-trend greeting snapshot")
	}
	// Callers cannot mutate the worker-owned reservation.
	*greeting.Sample["temperature_c"] = 99
	if pending, event := c.initial(epoch, 10); !pending || event != nil {
		t.Fatal("sent during greeting")
	}
	c.FinishGreeting(true)
	acknowledged := c.takeAcknowledged()
	if acknowledged == nil || *acknowledged.Sample["temperature_c"] != 24 {
		t.Fatal("reservation mutated")
	}
	d.Attempted(epoch, acknowledged, true)
	if d.metrics["temperature_c"].baselineAt != 1000 || d.lastAccepted != epoch {
		t.Fatal("greeting did not advance baseline/cooldown")
	}
	if pending, event := c.initial(epoch, 10); pending || event != nil {
		t.Fatal("duplicate initial report")
	}
	c.update(true, nil, nil)
	_, _, event := readyStartup(t)
	c.update(true, event, map[string]time.Time{"temperature_c": epoch.Add(time.Hour)})
	if pending, _ := c.initial(epoch.Add(time.Minute), 10); pending {
		t.Fatal("reconnect/config refresh replayed initial")
	}
}

func TestStartupColdBootAndDroppedRetry(t *testing.T) {
	c := NewStartupCoordinator()
	if c.ReserveGreeting(epoch) != nil {
		t.Fatal("cold greeting had data")
	}
	c.FinishGreeting(true)
	_, _, event := readyStartup(t)
	expires := map[string]time.Time{"temperature_c": epoch.Add(time.Hour)}
	c.update(true, event, expires)
	if pending, e := c.initial(epoch, 10); !pending || e == nil {
		t.Fatal("no initial report after plain greeting")
	}
	if _, e := c.initial(epoch.Add(9*time.Second), 10); e != nil {
		t.Fatal("dropped report retried too soon")
	}
	if _, e := c.initial(epoch.Add(10*time.Second), 10); e == nil {
		t.Fatal("dropped report not retried")
	}
	c.acceptedInitial()
	if pending, _ := c.initial(epoch.Add(time.Minute), 10); pending {
		t.Fatal("accepted queued/live report replayed")
	}
}

func TestStartupFailedGreetingAndPerMetricExpiry(t *testing.T) {
	c, _, event := readyStartup(t)
	event.Sample["co2_ppm"] = number(800)
	event.MetricTimestamps["co2_ppm"] = 1000
	c.update(true, event, map[string]time.Time{"temperature_c": epoch.Add(time.Second), "co2_ppm": epoch.Add(5 * time.Second)})
	greeting := c.ReserveGreeting(epoch.Add(2 * time.Second))
	if greeting == nil || greeting.Sample["temperature_c"] != nil || greeting.Sample["co2_ppm"] == nil {
		t.Fatal("per-metric expiry not applied")
	}
	c.FinishGreeting(false)
	if _, e := c.initial(epoch.Add(3*time.Second), 10); e == nil {
		t.Fatal("failed greeting consumed report")
	}
	c2, _, _ := readyStartup(t)
	if c2.ReserveGreeting(epoch.Add(time.Hour)) != nil {
		t.Fatal("stale cache included")
	}
	c2.update(false, event, nil)
	if c2.ReserveGreeting(epoch) != nil {
		t.Fatal("disabled initial report included")
	}
}

func TestStartupWarmupMetadataAndLegacy(t *testing.T) {
	for _, tc := range []struct {
		name     string
		evidence *float64
		ready    bool
	}{
		{"warm HAL", number(20), true}, {"cold HAL", number(0), false}, {"legacy", nil, false},
		{"negative", number(-1), false}, {"nan", number(math.NaN()), false}, {"infinite", number(math.Inf(1)), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := NewDetector(testSettings())
			s := sample(0, 24)
			s.ContinuousDataS = tc.evidence
			d.Observe(epoch, s)
			event, _ := d.startupSnapshot(epoch, s)
			if (event != nil) != tc.ready {
				t.Fatalf("ready=%v", event != nil)
			}
			if !tc.ready {
				for sec := 10; sec <= 20; sec += 10 {
					s = sample(sec, 24)
					d.Observe(epoch.Add(time.Duration(sec)*time.Second), s)
				}
				event, _ = d.startupSnapshot(epoch.Add(20*time.Second), s)
				if event == nil {
					t.Fatal("local warmup evidence ignored")
				}
			}
		})
	}
}

func TestStartupExcludesWarmingGasAndNoLaterInitial(t *testing.T) {
	cfg := testSettings()
	cfg.Metrics["voc_index"] = config.EnvironmentMetricRule{Delta: 50, WarmupS: 3600}
	d := NewDetector(cfg)
	s := sample(0, 24)
	s.ContinuousDataS = number(30)
	s.Sample["voc_index"] = number(120)
	d.Observe(epoch, s)
	event, expires := d.startupSnapshot(epoch, s)
	if event == nil || event.Sample["voc_index"] != nil {
		t.Fatal("warming gas included")
	}
	c := NewStartupCoordinator()
	c.update(true, event, expires)
	c.FinishGreeting(false)
	if _, e := c.initial(epoch, 10); e == nil {
		t.Fatal("ready temperature blocked by gas warmup")
	}
	c.acceptedInitial()
	s = sample(3600, 24)
	s.ContinuousDataS = number(3600)
	s.Sample["voc_index"] = number(120)
	d.Observe(epoch.Add(time.Hour), s)
	event, expires = d.startupSnapshot(epoch.Add(time.Hour), s)
	c.update(true, event, expires)
	if pending, _ := c.initial(epoch.Add(time.Hour), 10); pending {
		t.Fatal("later gas replayed initial report")
	}
}

func TestStartupConcurrentGreetingAndPolling(t *testing.T) {
	c, _, event := readyStartup(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				c.update(true, event, map[string]time.Time{"temperature_c": epoch.Add(time.Hour)})
				c.ReserveGreeting(epoch)
				c.initial(epoch, 10)
			}
		}()
	}
	c.FinishGreeting(true)
	wg.Wait()
}

func TestStartupContinuousEvidenceResetRestartsWarmup(t *testing.T) {
	d := NewDetector(testSettings())
	s := sample(0, 24)
	s.ContinuousDataS = number(100)
	d.Observe(epoch, s)
	s = sample(10, 24)
	s.ContinuousDataS = number(0)
	d.Observe(epoch.Add(10*time.Second), s)
	if event, _ := d.startupSnapshot(epoch.Add(10*time.Second), s); event != nil {
		t.Fatal("HAL reconnect reused previous warmup")
	}
}

func TestStartupCacheRespectsHALStaleDeadline(t *testing.T) {
	d := NewDetector(testSettings())
	s := sample(0, 24)
	s.ContinuousDataS = number(100)
	s.AgeS = number(2)
	s.Timing.StaleAfterS = 5
	d.Observe(epoch, s)
	event, expires := d.startupSnapshot(epoch, s)
	c := NewStartupCoordinator()
	c.update(true, event, expires)
	if c.ReserveGreeting(epoch.Add(4*time.Second)) != nil {
		t.Fatal("cache outlived HAL freshness deadline")
	}
}

func TestStartupWarmupEvidenceBelongsToProducingComponent(t *testing.T) {
	cfg := testSettings()
	cfg.Metrics["co2_ppm"] = config.EnvironmentMetricRule{Delta: 200, WarmupS: 20}
	d := NewDetector(cfg)
	s := componentSample(0, 0, 24, 800)
	s.ContinuousDataS = number(10000)
	sen := s.Components["sen55"]
	sen.ContinuousDataS = number(100)
	s.Components["sen55"] = sen
	co2 := s.Components["scd41"]
	co2.ContinuousDataS = number(0)
	s.Components["scd41"] = co2
	d.Observe(epoch, s)
	event, _ := d.startupSnapshot(epoch, s)
	if event == nil || event.Sample["temperature_c"] == nil || event.Sample["co2_ppm"] != nil {
		t.Fatal("one component's uptime warmed another")
	}
}
