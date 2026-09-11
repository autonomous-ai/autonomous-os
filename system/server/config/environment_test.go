package config

import (
	"encoding/json"
	"math"
	"testing"
)

func TestEnvironmentDefaultsAndPartialConfig(t *testing.T) {
	var c Config
	if err := json.Unmarshal([]byte(`{"environment":{"cooldown_s":0,"metrics":{"voc_index":{"delta":25}}}}`), &c); err != nil {
		t.Fatal(err)
	}
	settings := c.EnvironmentSettings()
	if settings.CooldownS != 0 || settings.EvaluateIntervalS != 10 || len(settings.Metrics) != 1 || settings.Metrics["voc_index"].WarmupS != 3600 {
		t.Fatalf("wrong effective config: %+v", settings)
	}
	settings.Metrics["voc_index"] = EnvironmentMetricRule{Delta: 999}
	if c.EnvironmentSettings().Metrics["voc_index"].Delta != 25 {
		t.Fatal("settings returned shared map")
	}
	var old Config
	if got := old.EnvironmentSettings(); !got.Enabled || !got.InitialReport || got.SustainS != 60 || len(got.Metrics) != 9 {
		t.Fatalf("old config defaults: %+v", got)
	}
	if Default().Environment == nil {
		t.Fatal("new config must persist editable defaults")
	}
}

func TestEnvironmentRejectsInvalidConfig(t *testing.T) {
	for _, body := range []string{
		`{"evaluate_interval_s":0}`, `{"sustain_s":1}`, `{"retry_interval_s":1}`,
		`{"cooldown_s":-1}`, `{"max_sample_age_s":0}`, `{"enabled":null}`, `{"initial_report":null}`, `{"initial_report":"false"}`,
		`{"metrics":{}}`, `{"metrics":null}`, `{"unknown":1}`,
		`{"metrics":{"co_ppm":{"delta":10}}}`, `{"metrics":{"voc_index":null}}`,
		`{"metrics":{"voc_index":{"delta":0}}}`, `{"metrics":{"voc_index":{"warmup_s":null}}}`,
		`{"metrics":{"voc_index":{"warmup_s":-1}}}`, `{"metrics":{"voc_index":{"typo":2}}}`,
	} {
		t.Run(body, func(t *testing.T) {
			var c EnvironmentConfig
			if err := json.Unmarshal([]byte(body), &c); err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
	c := DefaultEnvironmentConfig()
	c.EvaluateIntervalS = math.NaN()
	if c.Validate() == nil {
		t.Fatal("NaN accepted")
	}
}

func TestEnvironmentCO2DefaultsAndExplicitSubset(t *testing.T) {
	var c EnvironmentConfig
	if err := json.Unmarshal([]byte(`{}`), &c); err != nil {
		t.Fatal(err)
	}
	if c.Metrics["co2_ppm"] != (EnvironmentMetricRule{Delta: 200, WarmupS: 60}) {
		t.Fatal(c.Metrics)
	}
	if err := json.Unmarshal([]byte(`{"metrics":{"temperature_c":{"delta":3}}}`), &c); err != nil {
		t.Fatal(err)
	}
	if _, ok := c.Metrics["co2_ppm"]; ok {
		t.Fatal("explicit subset unexpectedly monitors CO2")
	}
	if err := json.Unmarshal([]byte(`{"metrics":{"co2_ppm":{"delta":300}}}`), &c); err != nil {
		t.Fatal(err)
	}
	if c.Metrics["co2_ppm"].WarmupS != 60 {
		t.Fatal(c.Metrics)
	}
}

func TestEnvironmentInitialReportCanBeDisabledIndependently(t *testing.T) {
	var c EnvironmentConfig
	if err := json.Unmarshal([]byte(`{"initial_report":false}`), &c); err != nil {
		t.Fatal(err)
	}
	if c.InitialReport || !c.Enabled || len(c.Metrics) != 9 {
		t.Fatalf("initial report toggle changed sensing policy: %+v", c)
	}
}
