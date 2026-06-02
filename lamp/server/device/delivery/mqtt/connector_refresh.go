package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const (
	// connectorRefreshInterval is how often the loop scans every registered
	// ConnectorWriter for expiring entries. Tight enough that a 1-hour token
	// refreshes well before lapsing (combined with the 10-minute skew below),
	// cheap because the common case finds nothing to do.
	connectorRefreshInterval = 3 * time.Minute

	// connectorRefreshSkew refreshes a token once it has less than this
	// remaining. Same value the OAuth path uses, kept consistent so behaviour
	// is predictable across both loops.
	connectorRefreshSkew = 10 * time.Minute

	// connectorRefreshTimeout bounds a single refresh round-trip to the backend.
	connectorRefreshTimeout = 30 * time.Second

	// connectorRefreshPath is appended to config.LLMBaseURL (minus /v1).
	connectorRefreshPath = "/connector/refresh-token"
)

// connectorRefreshResult is the subset of the backend response we apply
// locally. The backend proxies the rotation to the connector's token endpoint;
// it may return a rotated refresh_token (replaces the stored one) or omit it
// (existing refresh_token preserved).
type connectorRefreshResult struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int    `json:"expires_in"`
	Scope        string `json:"scope"`
}

// StartConnectorRefreshLoop runs until ctx is cancelled, periodically scanning
// the writer registry for tokens nearing expiry and refreshing them through the
// backend `/connector/refresh-token` endpoint. Per-tick panic recovery mirrors
// the OAuth refresh loop: a single bad iteration kills the tick, not the loop.
func (h *DeviceMQTTHandler) StartConnectorRefreshLoop(ctx context.Context) {
	h.safeConnectorRefreshTick(ctx) // eager first pass on boot
	ticker := time.NewTicker(connectorRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h.safeConnectorRefreshTick(ctx)
		}
	}
}

func (h *DeviceMQTTHandler) safeConnectorRefreshTick(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("connector-refresh: panic in refresh tick", "component", "mqtt", "panic", r)
		}
	}()
	h.refreshExpiringConnectors(ctx)
}

// refreshExpiringConnectors iterates every registered writer, asks for its
// refreshable entries, and proactively rotates any that fall inside the skew
// window. Each writer's failures are logged and skipped — a single connector
// must not block the others.
func (h *DeviceMQTTHandler) refreshExpiringConnectors(ctx context.Context) {
	if h.connectorWriters == nil {
		return
	}
	now := time.Now()
	for _, w := range h.uniqueWriters() {
		for _, target := range w.RefreshableEntries() {
			if !connectorNeedsRefresh(target, now, connectorRefreshSkew) {
				continue
			}
			res, err := h.requestConnectorTokenRefresh(ctx, target.Connector, target.RefreshToken)
			if err != nil {
				slog.Error("connector-refresh: refresh failed", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			creds, ok, err := h.loadRefreshedCreds(w, target.Connector, res, now)
			if err != nil {
				slog.Error("connector-refresh: build creds", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			if !ok {
				// Race: entry vanished between the listing and the load
				// (a concurrent connector.remove). Skip silently.
				continue
			}
			if err := w.Write(ctx, creds); err != nil {
				slog.Error("connector-refresh: persist refreshed", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			slog.Info("connector-refresh: token refreshed", "component", "mqtt", "connector", target.Connector, "expires_at", creds.ExpiresAt)
		}
	}
}

// uniqueWriters returns each writer once even if registered under multiple
// keys. The default writer occupies one slot under the "default" key;
// per-connector writers each get their own slot.
func (h *DeviceMQTTHandler) uniqueWriters() []ConnectorWriter {
	seen := make(map[ConnectorWriter]struct{}, len(h.connectorWriters))
	out := make([]ConnectorWriter, 0, len(h.connectorWriters))
	for _, w := range h.connectorWriters {
		if _, ok := seen[w]; ok {
			continue
		}
		seen[w] = struct{}{}
		out = append(out, w)
	}
	return out
}

// connectorNeedsRefresh reports whether the entry should be proactively
// refreshed. Only entries that carry a refresh_token are eligible (caller has
// already filtered). expires_at == 0 means "unknown" (BE didn't send
// expires_in, or token is non-expiring) and is skipped — no use hammering BE
// for tokens we can't reason about.
func connectorNeedsRefresh(t ConnectorRefreshTarget, now time.Time, skew time.Duration) bool {
	if t.RefreshToken == "" || t.ExpiresAt == 0 {
		return false
	}
	return now.Add(skew).Unix() >= t.ExpiresAt
}

// entryLoader is implemented by the per-connector MCP writer
// (mcpConnectorWriter) which keeps a full on-disk entry per connector.
// loadRefreshedCreds uses it to preserve fields the BE refresh response does
// NOT re-send — credentials map, client_id, scopes — so a token rotation
// doesn't blank them on disk. Writers without a token file (default writer →
// connectors.json) don't implement it and fall through to the minimal-creds
// path below.
type entryLoader interface {
	loadEntry(connector string) (ConnectorCreds, bool, error)
}

// loadRefreshedCreds builds the ConnectorCreds the writer needs for a re-Write.
// Reads the current on-disk entry (to preserve fields BE doesn't re-send) then
// layers in the freshly rotated access_token + token_type + expires_at (+
// refresh_token if rotated).
func (h *DeviceMQTTHandler) loadRefreshedCreds(w ConnectorWriter, connector string, res connectorRefreshResult, refreshedAt time.Time) (ConnectorCreds, bool, error) {
	if el, ok := w.(entryLoader); ok {
		base, present, err := el.loadEntry(connector)
		if err != nil {
			return ConnectorCreds{}, false, err
		}
		if !present {
			return ConnectorCreds{}, false, nil
		}
		base.AccessToken = res.AccessToken
		base.TokenType = firstNonEmpty(res.TokenType, base.TokenType)
		base.ExpiresAt = resolveExpiresAt(0, res.ExpiresIn, refreshedAt)
		if res.RefreshToken != "" {
			base.RefreshToken = res.RefreshToken
		}
		base.ObtainedAt = refreshedAt.Unix()
		return base, true, nil
	}
	// Fallback for generic writers: minimal creds. The writer's Write unions
	// with on-disk state, so zero scopes/client_id here are tolerable.
	return ConnectorCreds{
		Connector:    connector,
		AccessToken:  res.AccessToken,
		RefreshToken: res.RefreshToken,
		TokenType:    res.TokenType,
		ExpiresAt:    resolveExpiresAt(0, res.ExpiresIn, refreshedAt),
		ObtainedAt:   refreshedAt.Unix(),
	}, true, nil
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// requestConnectorTokenRefresh POSTs the refresh_token to the backend and
// returns the fresh token. The backend proxies the call to the connector's
// token endpoint — the device never touches the provider directly. Auth
// mirrors the OAuth refresh path: Bearer <LLMAPIKey> + X-Device-ID.
func (h *DeviceMQTTHandler) requestConnectorTokenRefresh(ctx context.Context, connector, refreshToken string) (connectorRefreshResult, error) {
	var out connectorRefreshResult

	base := strings.TrimRight(strings.TrimSpace(h.config.LLMBaseURL), "/")
	if base == "" {
		return out, errors.New("LLMBaseURL not configured")
	}
	// LLMBaseURL carries a trailing /v1 for OpenAI-compat LLM calls; autonomous
	// endpoints sit one level above. Mirror oauth_refresh.requestTokenRefresh.
	base = strings.TrimSuffix(base, "/v1")

	payload, err := json.Marshal(map[string]string{"connector": connector, "refresh_token": refreshToken})
	if err != nil {
		return out, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+connectorRefreshPath, bytes.NewReader(payload))
	if err != nil {
		return out, fmt.Errorf("new request: %w", err)
	}
	if key := strings.TrimSpace(h.config.LLMAPIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	if id := strings.TrimSpace(h.config.DeviceID); id != "" {
		req.Header.Set("X-Device-ID", id)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	client := &http.Client{Timeout: connectorRefreshTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return out, fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, fmt.Errorf("read body: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return out, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return out, fmt.Errorf("decode response: %w", err)
	}
	if out.AccessToken == "" {
		return out, errors.New("backend response missing access_token")
	}
	// expires_in must be strictly positive — see oauth_refresh.go's same guard:
	// a 0 would store ExpiresAt = now, spinning the loop every tick.
	if out.ExpiresIn <= 0 {
		return out, fmt.Errorf("backend response invalid expires_in=%d", out.ExpiresIn)
	}
	return out, nil
}
