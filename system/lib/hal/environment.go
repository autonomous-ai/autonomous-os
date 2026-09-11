package hal

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// GetEnvironmentStatus reads the model-independent environmental snapshot.
// Preserve measurement keys and nulls so new HAL sensor backends need no MQTT
// schema change. Status success means acquisition diagnostics were retrieved,
// not that every reading is fresh or available.
func GetEnvironmentStatus() (json.RawMessage, error) {
	resp, err := doGet("/environment/status")
	if err != nil {
		return nil, fmt.Errorf("get environment status: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("get environment status: HAL HTTP %d", resp.StatusCode)
	}
	const maxBytes = 64 * 1024
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read environment status: %w", err)
	}
	if len(body) > maxBytes {
		return nil, fmt.Errorf("environment status exceeds %d bytes", maxBytes)
	}
	var snapshot struct {
		State string `json:"state"`
		Stale *bool  `json:"stale"`
	}
	if err := json.Unmarshal(body, &snapshot); err != nil {
		return nil, fmt.Errorf("decode environment status: %w", err)
	}
	if snapshot.State == "" || snapshot.Stale == nil {
		return nil, fmt.Errorf("invalid environment status: state and stale are required")
	}
	return json.RawMessage(body), nil
}
