package hermes

import (
	"encoding/json"
	"reflect"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestHermesUsageCacheAccounting(t *testing.T) {
	tests := []struct {
		name string
		wire string
		want *domain.TokenUsage
	}{
		{"missing", `null`, nil},
		{"empty", `{}`, nil},
		{"legacy without details", `{"input_tokens":14097,"output_tokens":66,"total_tokens":14163}`, &domain.TokenUsage{InputTokens: 14097, OutputTokens: 66, TotalTokens: 14163}},
		{"device cache hit", `{"input_tokens":14097,"output_tokens":66,"total_tokens":14163,"input_tokens_details":{"cached_tokens":13824}}`, &domain.TokenUsage{InputTokens: 273, OutputTokens: 66, TotalTokens: 14163, CacheReadTokens: 13824}},
		{"cache read and write", `{"input_tokens":15000,"output_tokens":66,"total_tokens":15066,"input_tokens_details":{"cached_tokens":13824,"cache_write_tokens":1000}}`, &domain.TokenUsage{InputTokens: 176, OutputTokens: 66, TotalTokens: 15066, CacheReadTokens: 13824, CacheWriteTokens: 1000}},
		{"cache write only", `{"input_tokens":1000,"output_tokens":10,"total_tokens":1010,"input_tokens_details":{"cache_write_tokens":1000}}`, &domain.TokenUsage{OutputTokens: 10, TotalTokens: 1010, CacheWriteTokens: 1000}},
		{"negative details", `{"input_tokens":100,"output_tokens":10,"total_tokens":110,"input_tokens_details":{"cached_tokens":-2,"cache_write_tokens":-1}}`, &domain.TokenUsage{InputTokens: 100, OutputTokens: 10, TotalTokens: 110}},
		{"oversized cache read", `{"input_tokens":100,"output_tokens":10,"total_tokens":110,"input_tokens_details":{"cached_tokens":101}}`, &domain.TokenUsage{InputTokens: 100, OutputTokens: 10, TotalTokens: 110}},
		{"combined cache exceeds input", `{"input_tokens":100,"output_tokens":10,"total_tokens":110,"input_tokens_details":{"cached_tokens":80,"cache_write_tokens":30}}`, &domain.TokenUsage{InputTokens: 100, OutputTokens: 10, TotalTokens: 110}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var usage *hermesUsage
			if err := json.Unmarshal([]byte(tt.wire), &usage); err != nil {
				t.Fatal(err)
			}
			got := usage.toDomain()
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("usage = %+v, want %+v", got, tt.want)
			}
			if got != nil && got.InputTokens+got.CacheReadTokens+got.CacheWriteTokens+got.OutputTokens != got.TotalTokens {
				t.Fatalf("token buckets double-count or lose tokens: %+v", got)
			}
		})
	}
}

func TestHermesCompletedEventPreservesCacheForMonitor(t *testing.T) {
	s := &HermesService{config: &config.Config{}}
	result := &streamResult{DeviceRunID: "device-cache-test"}
	var events []domain.WSEvent
	s.translateSSE("response.completed", `{"response":{"id":"resp-cache-test","output":[],"usage":{"input_tokens":15000,"output_tokens":66,"total_tokens":15066,"input_tokens_details":{"cached_tokens":13824,"cache_write_tokens":1000}}}}`, func(evt domain.WSEvent) {
		events = append(events, evt)
	}, result)
	if len(events) != 2 || events[1].Event != "agent" {
		t.Fatalf("expected chat.final followed by lifecycle.end, got %+v", events)
	}
	var payload struct {
		RunID string `json:"runId"`
		Data  struct {
			Phase string            `json:"phase"`
			Usage domain.TokenUsage `json:"usage"`
		} `json:"data"`
	}
	if err := json.Unmarshal(events[1].Payload, &payload); err != nil {
		t.Fatal(err)
	}
	want := domain.TokenUsage{InputTokens: 176, OutputTokens: 66, TotalTokens: 15066, CacheReadTokens: 13824, CacheWriteTokens: 1000}
	if payload.RunID != result.DeviceRunID || payload.Data.Phase != "end" || payload.Data.Usage != want {
		t.Fatalf("monitor lifecycle usage lost cache: %+v", payload)
	}
}
