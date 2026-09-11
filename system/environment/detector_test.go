package environment

import (
	"math"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

func testSettings() config.EnvironmentConfig {
	c := config.DefaultEnvironmentConfig()
	c.EvaluateIntervalS = 10
	c.SustainS = 20
	c.CooldownS = 60
	c.RetryIntervalS = 10
	c.Metrics = map[string]config.EnvironmentMetricRule{"temperature_c": {Delta: 2, WarmupS: 20}}
	return c
}
func number(v float64) *float64 { return &v }

var epoch = time.Unix(1000, 0)

func sample(second int, value float64) Snapshot {
	return Snapshot{State: "ready", Enabled: true, AgeS: number(0), Sample: map[string]*float64{"timestamp": number(float64(1000 + second)), "temperature_c": number(value)}}
}
func observe(d *Detector, second int, value float64) *Event {
	return d.Observe(epoch.Add(time.Duration(second)*time.Second), sample(second, value))
}
func mustNoEvent(t *testing.T, e *Event) {
	t.Helper()
	if e != nil {
		t.Fatalf("unexpected event: %+v", e)
	}
}
func mustChange(t *testing.T, e *Event, previous, current float64) {
	t.Helper()
	if e == nil {
		t.Fatal("missing event")
	}
	v := e.Changes["temperature_c"]
	if v.Previous != previous || v.Current != current || v.Delta != current-previous {
		t.Fatalf("change: %+v", v)
	}
}

func TestDetectorWarmupSustainAndSpikeReset(t *testing.T) {
	d := NewDetector(testSettings())
	for _, row := range []struct {
		second int
		value  float64
	}{{0, 10}, {10, 30}, {20, 20}, {30, 23}, {40, 20}, {50, 23}, {60, 23}} {
		mustNoEvent(t, observe(d, row.second, row.value))
	}
	mustChange(t, observe(d, 70, 23), 20, 23)
}
func TestDetectorReversalRestartsSustain(t *testing.T) {
	d := NewDetector(testSettings())
	for _, row := range []struct {
		second int
		value  float64
	}{{0, 20}, {10, 20}, {20, 20}, {30, 23}, {40, 17}, {50, 17}} {
		mustNoEvent(t, observe(d, row.second, row.value))
	}
	mustChange(t, observe(d, 60, 17), 20, 17)
}
func TestDetectorDroppedRetryAndAcceptedBaselineCooldown(t *testing.T) {
	d := NewDetector(testSettings())
	for second := 0; second <= 20; second += 10 {
		mustNoEvent(t, observe(d, second, 20))
	}
	mustNoEvent(t, observe(d, 30, 23))
	mustNoEvent(t, observe(d, 40, 23))
	event := observe(d, 50, 23)
	mustChange(t, event, 20, 23)
	d.Attempted(epoch.Add(50*time.Second), event, false)
	mustNoEvent(t, observe(d, 55, 23))
	event = observe(d, 60, 23)
	mustChange(t, event, 20, 23)
	d.Attempted(epoch.Add(60*time.Second), event, true)
	mustNoEvent(t, observe(d, 70, 23))
	for second := 80; second <= 110; second += 10 {
		mustNoEvent(t, observe(d, second, 20))
	}
	mustChange(t, observe(d, 120, 20), 23, 20)
}
func TestDetectorInvalidReadingsResetWarmup(t *testing.T) {
	cases := map[string]func(*Snapshot){
		"device fault": func(s *Snapshot) { s.Sample["device_status"] = number(1) },
		"disabled":     func(s *Snapshot) { s.Enabled = false }, "error": func(s *Snapshot) { s.State = "error" },
		"stale": func(s *Snapshot) { s.Stale = true }, "old": func(s *Snapshot) { s.AgeS = number(11) },
		"unknown age": func(s *Snapshot) { s.AgeS = nil }, "negative age": func(s *Snapshot) { s.AgeS = number(-1) },
		"null sample": func(s *Snapshot) { s.Sample = nil }, "null metric": func(s *Snapshot) { s.Sample["temperature_c"] = nil },
		"invalid metric":    func(s *Snapshot) { s.Sample["temperature_c"] = number(math.NaN()) },
		"invalid timestamp": func(s *Snapshot) { s.Sample["timestamp"] = number(math.Inf(1)) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			d := NewDetector(testSettings())
			for second := 0; second <= 20; second += 10 {
				observe(d, second, 20)
			}
			observe(d, 30, 23)
			bad := sample(40, 23)
			mutate(&bad)
			mustNoEvent(t, d.Observe(epoch.Add(40*time.Second), bad))
			for second := 50; second <= 70; second += 10 {
				mustNoEvent(t, observe(d, second, 23))
			}
			mustNoEvent(t, observe(d, 80, 26))
			mustNoEvent(t, observe(d, 90, 26))
			mustChange(t, observe(d, 100, 26), 23, 26)
		})
	}
}
func TestDetectorOutageRecoveryAndRepeatedSamples(t *testing.T) {
	d := NewDetector(testSettings())
	for second := 0; second <= 20; second += 10 {
		observe(d, second, 20)
	}
	observe(d, 30, 23)
	mustNoEvent(t, d.Observe(epoch.Add(40*time.Second), sample(30, 23)))
	// A repeated timestamp cannot count as an independent sustained observation.
	mustNoEvent(t, d.Observe(epoch.Add(50*time.Second), sample(30, 23)))
	// Long gap invalidates prior baseline, even if the next sample looks fresh.
	for second := 100; second <= 120; second += 10 {
		mustNoEvent(t, observe(d, second, 23))
	}
	mustNoEvent(t, observe(d, 130, 26))
	mustNoEvent(t, observe(d, 140, 26))
	mustChange(t, observe(d, 150, 26), 23, 26)
}
func TestDetectorNullMetricDoesNotResetOtherMetrics(t *testing.T) {
	c := testSettings()
	c.Metrics["humidity_pct"] = config.EnvironmentMetricRule{Delta: 10, WarmupS: 0}
	d := NewDetector(c)
	for second := 0; second <= 20; second += 10 {
		s := sample(second, 20)
		s.Sample["humidity_pct"] = number(50)
		mustNoEvent(t, d.Observe(epoch.Add(time.Duration(second)*time.Second), s))
	}
	for second := 30; second <= 50; second += 10 {
		s := sample(second, 23)
		s.Sample["humidity_pct"] = nil
		e := d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
		if second < 50 {
			mustNoEvent(t, e)
		} else {
			mustChange(t, e, 20, 23)
		}
	}
}
