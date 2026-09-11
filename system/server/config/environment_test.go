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
	if got := old.EnvironmentSettings(); !got.Enabled || got.SustainS != 60 || len(got.Metrics) != 8 {
		t.Fatalf("old config defaults: %+v", got)
	}
	if Default().Environment == nil {
		t.Fatal("new config must persist editable defaults")
	}
}

func TestEnvironmentRejectsInvalidConfig(t *testing.T) {
	for _, body := range []string{
		`{"evaluate_interval_s":0}`, `{"sustain_s":1}`, `{"retry_interval_s":1}`,
		`{"cooldown_s":-1}`, `{"max_sample_age_s":0}`, `{"enabled":null}`,
		`{"metrics":{}}`, `{"metrics":null}`, `{"unknown":1}`,
		`{"metrics":{"co2_ppm":{"delta":10}}}`, `{"metrics":{"voc_index":null}}`,
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
