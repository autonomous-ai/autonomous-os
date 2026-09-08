package domain

import (
	"encoding/json"
	"math"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestTTSSpeedValidation(t *testing.T) {
	if err := ValidateTTSSpeed(nil); err != nil {
		t.Fatal(err)
	}
	for _, speed := range []float64{0.7, 1, 1.2} {
		if err := ValidateTTSSpeed(&speed); err != nil {
			t.Fatal(err)
		}
	}
	for _, speed := range []float64{0, -1, 0.69, 1.21, math.NaN(), math.Inf(1), math.Inf(-1)} {
		if ValidateTTSSpeed(&speed) == nil {
			t.Fatalf("accepted %v", speed)
		}
	}
}

func TestTTSSpeedMQTTContract(t *testing.T) {
	t.Setenv("HAL_TTS_SPEED", "")
	for _, payload := range []string{`{}`, `{"speed":null}`, `{"speed":1.2}`} {
		var req MQTTTTSSetData
		if err := json.Unmarshal([]byte(payload), &req); err != nil {
			t.Fatal(err)
		}
		if err := ValidateTTSSpeed(req.Speed); err != nil {
			t.Fatal(err)
		}
		if payload == `{"speed":1.2}` && (req.Speed == nil || *req.Speed != 1.2) {
			t.Fatal("speed lost")
		}
		if payload != `{"speed":1.2}` && req.Speed != nil {
			t.Fatal("omission must preserve saved speed")
		}
	}
	cfg := &config.Config{}
	if got := NewMQTTInfoResponse(cfg, "info", "test").TTSSpeed; got != 1 {
		t.Fatalf("default %v", got)
	}
	speed := 1.2
	cfg.TTSSpeed = &speed
	raw, err := json.Marshal(NewMQTTInfoResponse(cfg, "info", "test"))
	if err != nil {
		t.Fatal(err)
	}
	var msg map[string]any
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatal(err)
	}
	if msg["tts_speed"] != 1.2 {
		t.Fatalf("info speed missing: %s", raw)
	}
}
