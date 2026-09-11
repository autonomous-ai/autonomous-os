// Package environment turns fresh HAL snapshots into sustained-change events.
package environment

import (
	"encoding/json"
	"math"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

type Snapshot struct {
	State   string              `json:"state"`
	Enabled bool                `json:"enabled"`
	Stale   bool                `json:"stale"`
	AgeS    *float64            `json:"age_s"`
	Sample  map[string]*float64 `json:"sample"`
}

type Change struct {
	Previous   float64 `json:"previous"`
	PreviousAt float64 `json:"previous_at"`
	Current    float64 `json:"current"`
	Delta      float64 `json:"delta"`
}

type Event struct {
	ObservedAt float64             `json:"observed_at"`
	Sample     map[string]*float64 `json:"sample"`
	Changes    map[string]Change   `json:"changes"`
	SustainedS float64             `json:"sustained_s"`
}

func (e Event) Message() string {
	body, _ := json.Marshal(e)
	return string(body)
}

type metricState struct {
	first      time.Time
	baseline   *float64
	baselineAt float64
	since      time.Time
	direction  int
}

// Detector is owned by one polling goroutine. Baselines change only after the
// event was accepted by the dispatch layer, never after a dropped HTTP 200.
type Detector struct {
	cfg          config.EnvironmentConfig
	metrics      map[string]*metricState
	lastSample   float64
	lastFresh    time.Time
	lastAccepted time.Time
	lastAttempt  time.Time
}

func NewDetector(cfg config.EnvironmentConfig) *Detector {
	return &Detector{cfg: cfg.Clone(), metrics: make(map[string]*metricState)}
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

func (d *Detector) ResetReadings() {
	d.metrics = make(map[string]*metricState)
	d.lastSample = 0
	d.lastFresh = time.Time{}
}

func (d *Detector) Observe(now time.Time, s Snapshot) *Event {
	stamp := s.Sample["timestamp"]
	if status := s.Sample["device_status"]; status != nil && *status != 0 {
		d.ResetReadings()
		return nil
	}
	if !s.Enabled || s.State != "ready" || s.Stale || s.AgeS == nil || !finite(*s.AgeS) || *s.AgeS < 0 || *s.AgeS > d.cfg.MaxSampleAgeS || stamp == nil || !finite(*stamp) || *stamp <= 0 {
		d.ResetReadings()
		return nil
	}
	// An outage or a restarted sensor cannot inherit an in-progress change.
	if !d.lastFresh.IsZero() && now.Sub(d.lastFresh).Seconds() > 2*d.cfg.EvaluateIntervalS {
		d.ResetReadings()
	}
	if *stamp <= d.lastSample {
		return nil
	}
	d.lastSample = *stamp
	d.lastFresh = now
	event := &Event{ObservedAt: *stamp, Sample: s.Sample, Changes: make(map[string]Change), SustainedS: d.cfg.SustainS}
	for key, rule := range d.cfg.Metrics {
		value := s.Sample[key]
		if value == nil || !finite(*value) {
			delete(d.metrics, key)
			continue
		}
		state := d.metrics[key]
		if state == nil {
			state = &metricState{first: now}
			d.metrics[key] = state
		}
		if now.Sub(state.first).Seconds() < rule.WarmupS {
			continue
		}
		if state.baseline == nil {
			v := *value
			state.baseline = &v
			state.baselineAt = *stamp
			continue
		}
		delta := *value - *state.baseline
		if math.Abs(delta) < rule.Delta {
			state.since = time.Time{}
			state.direction = 0
			continue
		}
		direction := 1
		if delta < 0 {
			direction = -1
		}
		if state.since.IsZero() || direction != state.direction {
			state.since, state.direction = now, direction
			continue
		}
		if now.Sub(state.since).Seconds() >= d.cfg.SustainS {
			event.Changes[key] = Change{Previous: *state.baseline, PreviousAt: state.baselineAt, Current: *value, Delta: delta}
		}
	}
	if len(event.Changes) == 0 || (!d.lastAccepted.IsZero() && now.Sub(d.lastAccepted).Seconds() < d.cfg.CooldownS) || (!d.lastAttempt.IsZero() && now.Sub(d.lastAttempt).Seconds() < d.cfg.RetryIntervalS) {
		return nil
	}
	return event
}

func (d *Detector) Attempted(now time.Time, event *Event, accepted bool) {
	d.lastAttempt = now
	if !accepted {
		return
	}
	d.lastAccepted = now
	for key, change := range event.Changes {
		if state := d.metrics[key]; state != nil {
			v := change.Current
			state.baseline = &v
			state.baselineAt = event.ObservedAt
			state.since = time.Time{}
			state.direction = 0
		}
	}
}
