package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
)

// EnvironmentMetricRule defines a change threshold, not a health limit.
type EnvironmentMetricRule struct {
	Delta   float64 `json:"delta"`
	WarmupS float64 `json:"warmup_s"`
}

// EnvironmentConfig controls OS interpretation separately from HAL acquisition.
type EnvironmentConfig struct {
	Enabled           bool                             `json:"enabled"`
	InitialReport     bool                             `json:"initial_report"`
	EvaluateIntervalS float64                          `json:"evaluate_interval_s"`
	SustainS          float64                          `json:"sustain_s"`
	CooldownS         float64                          `json:"cooldown_s"`
	RetryIntervalS    float64                          `json:"retry_interval_s"`
	MaxSampleAgeS     float64                          `json:"max_sample_age_s"`
	Metrics           map[string]EnvironmentMetricRule `json:"metrics"`
}

func DefaultEnvironmentConfig() EnvironmentConfig {
	return EnvironmentConfig{
		Enabled: true, InitialReport: true, EvaluateIntervalS: 10, SustainS: 60, CooldownS: 900,
		RetryIntervalS: 60, MaxSampleAgeS: 10,
		Metrics: map[string]EnvironmentMetricRule{
			"pm1_0_ug_m3": {10, 60}, "pm2_5_ug_m3": {10, 60},
			"pm4_0_ug_m3": {15, 60}, "pm10_ug_m3": {15, 60},
			"temperature_c": {2, 60}, "humidity_pct": {10, 60},
			"voc_index": {50, 3600}, "nox_index": {20, 21600},
			"co2_ppm": {200, 60},
		},
	}
}

// UnmarshalJSON fills omitted top-level fields with defaults. A supplied metrics
// map replaces the default map, allowing an operator to monitor a subset.
func (c *EnvironmentConfig) UnmarshalJSON(data []byte) error {
	type plain EnvironmentConfig
	v := plain(DefaultEnvironmentConfig())
	// Decode maps from empty so omitted metrics can genuinely be disabled.
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name, raw := range fields {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("environment.%s cannot be null", name)
		}
	}
	if _, ok := fields["metrics"]; ok {
		v.Metrics = nil
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&v); err != nil {
		return err
	}
	if raw, ok := fields["metrics"]; ok {
		var rules map[string]map[string]json.RawMessage
		if err := json.Unmarshal(raw, &rules); err != nil {
			return err
		}
		for key, fields := range rules {
			rule := DefaultEnvironmentConfig().Metrics[key]
			for name, raw := range fields {
				if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
					return fmt.Errorf("environment.metrics.%s.%s cannot be null", key, name)
				}
			}
			if fields == nil {
				return fmt.Errorf("environment.metrics.%s cannot be null", key)
			}
			body, _ := json.Marshal(fields)
			if err := json.Unmarshal(body, &rule); err != nil {
				return err
			}
			v.Metrics[key] = rule
		}
	}
	*c = EnvironmentConfig(v)
	return c.Validate()
}

func (c EnvironmentConfig) Validate() error {
	for name, value := range map[string]float64{
		"evaluate_interval_s": c.EvaluateIntervalS, "sustain_s": c.SustainS,
		"retry_interval_s": c.RetryIntervalS, "max_sample_age_s": c.MaxSampleAgeS,
	} {
		if math.IsNaN(value) || math.IsInf(value, 0) || value < 1 || value > 86400 {
			return fmt.Errorf("environment.%s must be between 1 and 86400 seconds", name)
		}
	}
	if math.IsNaN(c.CooldownS) || math.IsInf(c.CooldownS, 0) || c.CooldownS < 0 || c.CooldownS > 604800 {
		return fmt.Errorf("environment.cooldown_s must be between 0 and 604800 seconds")
	}
	if c.SustainS < c.EvaluateIntervalS || c.RetryIntervalS < c.EvaluateIntervalS {
		return fmt.Errorf("environment sustain_s and retry_interval_s must be at least evaluate_interval_s")
	}
	known := DefaultEnvironmentConfig().Metrics
	if len(c.Metrics) == 0 {
		return fmt.Errorf("environment.metrics must contain at least one metric")
	}
	for name, rule := range c.Metrics {
		if _, ok := known[name]; !ok {
			return fmt.Errorf("unknown environment metric %q", name)
		}
		if math.IsNaN(rule.Delta) || math.IsInf(rule.Delta, 0) || rule.Delta <= 0 {
			return fmt.Errorf("environment metric %s delta must be finite and positive", name)
		}
		if math.IsNaN(rule.WarmupS) || math.IsInf(rule.WarmupS, 0) || rule.WarmupS < 0 || rule.WarmupS > 86400 {
			return fmt.Errorf("environment metric %s warmup_s must be between 0 and 86400", name)
		}
	}
	return nil
}

func (c EnvironmentConfig) Clone() EnvironmentConfig {
	copy := c
	copy.Metrics = make(map[string]EnvironmentMetricRule, len(c.Metrics))
	for key, value := range c.Metrics {
		copy.Metrics[key] = value
	}
	return copy
}
