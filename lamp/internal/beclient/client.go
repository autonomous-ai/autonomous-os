package beclient

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"go-lamp.autonomous.ai/server/config"
)

const (
	// DefaultTimeout is the HTTP client timeout for API requests.
	DefaultTimeout = 15 * time.Second
	// StatusReportInterval is how often to ping status to the backend.
	StatusReportInterval = 15 * time.Second
)

// Client calls the autonomous backend API to report setup status and device status.
// Base URL is read from config.LLMBaseURL on each Ping.
type Client struct {
	config     *config.Config
	httpClient *http.Client
}

// New creates a new BE client. Base URL is read from cfg.LLMBaseURL on each request.
func New(cfg *config.Config) *Client {
	return &Client{
		config: cfg,
		httpClient: &http.Client{
			Timeout: DefaultTimeout,
		},
	}
}

// Ping notifies the backend. Uses LLM API key as Bearer token. Returns the backend response if available.
// Appends ?mqtt=true when MQTT is not yet configured, signaling the backend to include MQTT config in the response.
func (c *Client) Ping(token string, payload PingPayload) (*PingResponse, error) {
	base := strings.TrimSuffix(strings.TrimSpace(c.config.LLMBaseURL), "/")
	if base == "" || token == "" {
		return nil, nil
	}
	// LLMBaseURL is configured with a trailing /v1 for OpenAI-compat LLM calls
	// (e.g. {base}/chat/completions). The autonomous /ping endpoint lives one
	// level above that — POST /api/v1/ai/ping (per docs/mqtt_specs_autonomous.md).
	// Strip a single trailing /v1 so we hit the correct route.
	base = strings.TrimSuffix(base, "/v1")
	pingURL := base + "/ping"
	if strings.TrimSpace(c.config.MQTTEndpoint) == "" {
		pingURL += "?mqtt=true"
	}
	body, _ := json.Marshal(payload)
	slog.Debug("pinging backend", "component", "beclient", "url", pingURL, "body", string(body))
	return c.postWithAuth(pingURL, token, payload)
}

// PingPayload is the ping body.
type PingPayload struct {
	Status         string `json:"status,omitempty"`
	SetupCompleted bool   `json:"setup_completed,omitempty"`
	Mac            string `json:"mac,omitempty"`     // Hardware ID (Lamp-XXXX from Pi serial)
	Version        string `json:"version,omitempty"` // App version for OTA comparison
}

// MQTTConfig holds MQTT broker configuration from the backend.
// Field names match the server spec (docs/mqtt_specs_autonomous.md).
type MQTTConfig struct {
	Endpoint  string `json:"mqtt_server,omitempty"`
	Port      string `json:"mqtt_port,omitempty"`
	Username  string `json:"mqtt_usr,omitempty"`
	Password  string `json:"mqtt_pwd,omitempty"`
	FaChannel string `json:"fa_channel,omitempty"`
	FdChannel string `json:"fd_channel,omitempty"`
}

// PingResponse is the backend response to a ping.
// Format: {"status": "ok", "device_id": "...", "mqtt": {...}}
type PingResponse struct {
	Status   string      `json:"status"`
	DeviceID string      `json:"device_id,omitempty"`
	MQTT     *MQTTConfig `json:"mqtt,omitempty"`
}

// HasMQTT returns true if the response contains MQTT configuration.
func (r *PingResponse) HasMQTT() bool {
	return r != nil && r.MQTT != nil && strings.TrimSpace(r.MQTT.Endpoint) != ""
}

// GetMQTT returns the MQTT config or nil.
func (r *PingResponse) GetMQTT() *MQTTConfig {
	if r == nil {
		return nil
	}
	return r.MQTT
}

func (c *Client) postWithAuth(reqURL, bearerToken string, body any) (*PingResponse, error) {
	var bodyReader *bytes.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	} else {
		bodyReader = bytes.NewReader([]byte("{}"))
	}
	req, err := http.NewRequest(http.MethodPost, reqURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+bearerToken)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request %s: %w", reqURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("request %s: status %d", reqURL, resp.StatusCode)
	}

	var pingResp PingResponse
	if err := json.NewDecoder(resp.Body).Decode(&pingResp); err != nil {
		// Response body is optional; ignore decode errors
		return nil, nil
	}
	return &pingResp, nil
}

// PingSafe logs errors but does not propagate them. Returns the response if available.
func (c *Client) PingSafe(token string, payload PingPayload) *PingResponse {
	resp, err := c.Ping(token, payload)
	if err != nil {
		slog.Error("ping failed", "component", "beclient", "error", err)
	}
	return resp
}
