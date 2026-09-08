package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

type speedTransport func(*http.Request) (*http.Response, error)

func (f speedTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestTTSSpeedTransport(t *testing.T) {
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	var path string
	var payload map[string]any
	http.DefaultTransport = speedTransport(func(r *http.Request) (*http.Response, error) {
		path = r.URL.Path
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{}`)), Header: make(http.Header)}, nil
	})
	if err := ApplyTTSConfig("elevenlabs", "voice", "key", "https://proxy.example", 1.2); err != nil {
		t.Fatal(err)
	}
	if path != "/voice/tts/config" || payload["speed"] != 1.2 {
		t.Fatalf("live %s %#v", path, payload)
	}
	payload = nil
	if err := StartVoice(VoiceStartConfig{TTSSpeed: 1.0}); err != nil {
		t.Fatal(err)
	}
	if path != "/voice/start" || payload["tts_speed"] != 1.0 {
		t.Fatalf("start %s %#v", path, payload)
	}
	payload = nil
	speed := 0.7
	if err := SpeakPreview("hello", "voice", "elevenlabs", "key", "", &speed); err != nil {
		t.Fatal(err)
	}
	if path != "/voice/speak" || payload["speed"] != 0.7 {
		t.Fatalf("preview %s %#v", path, payload)
	}
	payload = nil
	if err := SpeakPreview("hello", "", "", "", "", nil); err != nil {
		t.Fatal(err)
	}
	if _, found := payload["speed"]; found {
		t.Fatal("omitted preview speed must retain HAL default")
	}
}
