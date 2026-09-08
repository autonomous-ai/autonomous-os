// Package analytics ships events to Autonomous Analytics (AA), the same
// event_tracking backend the web app and the mobile app post to. Events land
// in one warehouse and are sliced by `platform` — "device" for us.
package analytics

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/joho/godotenv"
)

const (
	envFile   = "/opt/hal/.env"
	envKey    = "AUTONOMOUS_ANALYTICS_ID"
	envKeyURL = "AUTONOMOUS_ANALYTICS_URL"
	platform  = "device"
)

var (
	client = &http.Client{Timeout: 10 * time.Second}

	once      sync.Once
	apiKey    string
	fileURL   string // AUTONOMOUS_ANALYTICS_URL as read from envFile
	pseudoID  string
	sessionID string
)

func initOnce() {
	once.Do(func() {
		// os-server already godotenv.Load()s /opt/hal/.env at startup; read
		// the file directly as a fallback for callers that don't (tests, CLI).
		apiKey = os.Getenv(envKey)
		// fileURL holds ONLY what the file says. The process env is read live
		// in Endpoint() instead of being cached here: caching it would keep a
		// stale endpoint alive after the variable is cleared, which reads as
		// "analytics is still on" when it is not.
		if kv, err := godotenv.Read(envFile); err == nil {
			if apiKey == "" {
				apiKey = kv[envKey]
			}
			fileURL = kv[envKeyURL]
		}
		// Stable per-device identity: hostname is what the fleet is named by.
		pseudoID, _ = os.Hostname()
		// One session per process run — a device process is the session.
		sessionID = fmt.Sprintf("%s-%d", pseudoID, time.Now().Unix())
	})
}

// TrackEvent posts one event. params are flattened into AA's event_params
// list. Returns an error so callers can log it; analytics must never fail the
// feature that triggered it, so callers should not propagate it.
func TrackEvent(ctx context.Context, name string, params map[string]any) error {
	initOnce()
	url := Endpoint()
	if url == "" {
		return fmt.Errorf("analytics: %s not set in %s", envKeyURL, envFile)
	}
	if apiKey == "" {
		return fmt.Errorf("analytics: %s not set in %s", envKey, envFile)
	}

	eventParams := make([]map[string]string, 0, len(params))
	for k, v := range params {
		if v == nil {
			continue
		}
		eventParams = append(eventParams, map[string]string{
			"key":   k,
			"value": fmt.Sprint(v),
		})
	}

	body, err := json.Marshal(map[string]any{
		"event_name":      name,
		"event_timestamp": time.Now().Unix(),
		"data": map[string]any{
			"session_id":     sessionID,
			"user_pseudo_id": pseudoID,
			"platform":       platform,
			"event_params":   eventParams,
		},
	})
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", apiKey)

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("post event: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("post event: status %d", resp.StatusCode)
	}
	return nil
}

// Endpoint is where events are posted: AUTONOMOUS_ANALYTICS_URL from the
// process env first (what tests set), then the same key in the body's
// /opt/hal/.env. Read per call so a test can point it at a local server.
//
// Empty means analytics is not configured on this body — that is the OFF
// switch. There is deliberately no built-in default: a device sends events
// only to an endpoint someone wrote down.
func Endpoint() string {
	initOnce()
	if u := os.Getenv(envKeyURL); u != "" {
		return u
	}
	return fileURL
}
