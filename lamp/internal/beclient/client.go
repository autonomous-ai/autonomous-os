package beclient

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
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
	// cachedSlackTeamID holds the Slack workspace ID resolved on-device via
	// slack.auth.test against the bot_token in openclaw.json. Written by the
	// status reporter, read by the ping loop. Empty when slack isn't configured.
	cachedSlackTeamID atomic.Value // string
}

// New creates a new BE client. Base URL is read from cfg.LLMBaseURL on each request.
func New(cfg *config.Config) *Client {
	c := &Client{
		config: cfg,
		httpClient: &http.Client{
			Timeout: DefaultTimeout,
		},
	}
	c.cachedSlackTeamID.Store("")
	return c
}

// SetSlackTeamID records the Slack workspace ID resolved for the device's
// bot_token. Idempotent — calling with the same value is a no-op.
func (c *Client) SetSlackTeamID(v string) {
	c.cachedSlackTeamID.Store(v)
}

// SlackTeamID returns the cached Slack workspace ID. Empty string when not
// yet resolved or no slack channel is configured.
func (c *Client) SlackTeamID() string {
	v, _ := c.cachedSlackTeamID.Load().(string)
	return v
}

// ResolveSlackTeamIDFromConfig is a one-shot resolver: if the cache is empty
// AND the device has a slack bot_token in openclaw.json, it POSTs to
// slack.com/api/auth.test, parses the team_id, and caches it. Idempotent and
// safe to call on every status tick — once the cache is populated, returns
// immediately. Failures are silent (logged) so a transient Slack outage
// doesn't poison the cache; the next tick retries.
func (c *Client) ResolveSlackTeamIDFromConfig(configDir string) {
	if c.SlackTeamID() != "" {
		return
	}
	botToken := readSlackBotTokenFromConfig(configDir)
	if botToken == "" {
		return // slack not configured on this device — nothing to resolve
	}
	teamID, err := slackAuthTest(c.httpClient, botToken)
	if err != nil {
		slog.Debug("slack auth.test failed (will retry next tick)", "component", "beclient", "error", err)
		return
	}
	if teamID == "" {
		return
	}
	c.SetSlackTeamID(teamID)
	slog.Info("resolved slack team_id for ping payload", "component", "beclient", "team_id", teamID)
}

// readSlackBotTokenFromConfig reads channels.slack.botToken from the device's
// openclaw.json. Returns empty string if the file is unreadable, slack isn't
// configured, or botToken is empty — never errors (silent miss is fine).
func readSlackBotTokenFromConfig(configDir string) string {
	if configDir == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(configDir, "openclaw.json"))
	if err != nil {
		return ""
	}
	var doc struct {
		Channels struct {
			Slack struct {
				BotToken string `json:"botToken"`
			} `json:"slack"`
		} `json:"channels"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return ""
	}
	return doc.Channels.Slack.BotToken
}

// slackAuthTest POSTs to https://slack.com/api/auth.test with the bot token
// and returns team_id from the response. Reuses the same httpClient that
// ping uses so timeouts are bounded and metrics are unified.
func slackAuthTest(httpClient *http.Client, botToken string) (string, error) {
	req, err := http.NewRequest(http.MethodPost, "https://slack.com/api/auth.test", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+botToken)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("auth.test http %d", resp.StatusCode)
	}
	var out struct {
		OK     bool   `json:"ok"`
		Error  string `json:"error,omitempty"`
		TeamID string `json:"team_id"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", err
	}
	if !out.OK {
		return "", fmt.Errorf("auth.test not ok: %s", out.Error)
	}
	return out.TeamID, nil
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
	// SlackTeamID is the Slack workspace this device's bot is installed in,
	// resolved on-device via slack.auth.test against the stored bot_token
	// (cached on Client). Sent on every ping so the backend's
	// slack:team:<id> → device index self-heals — the proxy uses it to route
	// inbound Slack events back to this device's MQTT topic. Empty when the
	// slack channel isn't configured on this device.
	SlackTeamID string `json:"slack_team_id,omitempty"`
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
