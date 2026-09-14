package mqtthandler

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// connectorHandlerTimeout caps how long the per-write goroutine waits for the
// writer (which may restart openclaw — ~30-60s on a healthy Pi). Generous so
// a slow restart doesn't surface as `failure`; tight enough that a totally
// stuck restart can't leak goroutines.
const connectorHandlerTimeout = 2 * time.Minute

// handleConnectorSet handles the (privacy-fetched) payload for
// kind="connector.set.<code>".
//
//  1. ack `starting` on the broker callback goroutine so the BE knows the
//     envelope was received and can stop retrying.
//  2. async: parse, validate, look up writer by `connector` code (or default),
//     call writer.Write. Terminal status (`success`/`failure`) published from
//     the goroutine.
//
// Validation per contract: `connector` and `auth_type` are required.
// Unknown connector code falls through to the default writer (not an error).
func (h *DeviceMQTTHandler) handleConnectorSet(env domain.MQTTDataCommand) error {
	if err := h.publishDataResult(env.Kind, "starting", "", nil); err != nil {
		slog.Error("connector.set: ack publish failed", "component", "mqtt", "kind", env.Kind, "error", err)
	}

	go h.runConnectorSet(env)
	return nil
}

// staticConnectorCredential reports whether the payload carries a non-expiring
// secret (app password / static API key) rather than an OAuth access token.
// Mirrors connectorAuthHeader's rule — whichever field is populated is the
// credential, not the auth_type string — with auth_type "pat" as the backend's
// explicit hint for the case where neither token field is set.
func staticConnectorCredential(authType, accessToken, apiKey string) bool {
	if accessToken != "" {
		return false
	}
	return apiKey != "" || strings.EqualFold(strings.TrimSpace(authType), "pat")
}

// resolveConnectorExpiresAt is resolveExpiresAt with the static-credential
// carve-out. An app password or API key never expires and nothing rotates it
// (connectorNeedsRefresh skips expires_at == 0), so stamping it with the OAuth
// default lifetime would make it read as expired an hour after it was stored —
// with no loop to correct it. Persist 0 ("no expiry") when the backend sent no
// expiry info for such a credential.
func resolveConnectorExpiresAt(authType, accessToken, apiKey string, expiresAt int64, expiresIn int, now time.Time) int64 {
	if expiresAt <= 0 && expiresIn <= 0 && staticConnectorCredential(authType, accessToken, apiKey) {
		return 0
	}
	return resolveExpiresAt(expiresAt, expiresIn, now)
}

func (h *DeviceMQTTHandler) runConnectorSet(env domain.MQTTDataCommand) {
	var req domain.MQTTConnectorSetData
	if len(env.Data) == 0 {
		_ = h.publishDataResult(env.Kind, "failure", "invalid "+env.Kind+" data: empty", nil)
		return
	}
	if err := json.Unmarshal(env.Data, &req); err != nil {
		_ = h.publishDataResult(env.Kind, "failure", "invalid "+env.Kind+" data: "+err.Error(), nil)
		return
	}
	if req.Connector == "" {
		_ = h.publishDataResult(env.Kind, "failure", "connector is required", nil)
		return
	}
	if req.AuthType == "" {
		_ = h.publishDataResult(env.Kind, "failure", "auth_type is required", nil)
		return
	}

	now := time.Now()
	creds := ConnectorCreds{
		Connector:    req.Connector,
		AuthType:     req.AuthType,
		AccessToken:  req.AccessToken,
		RefreshToken: req.RefreshToken,
		TokenType:    req.TokenType,
		// Wire ships expires_in (seconds from now); persist absolute expires_at.
		// resolveConnectorExpiresAt treats (existing=0, in>0) as "compute fresh
		// from now", and stores 0 for a credential that never expires.
		ExpiresAt: resolveConnectorExpiresAt(
			req.AuthType, req.AccessToken, req.APIKey, req.ExpiresAt, req.ExpiresIn, now,
		),
		APIKey:      req.APIKey,
		Scopes:      req.Scopes,
		UserEmail:   req.UserEmail,
		ClientID:    req.ClientID,
		Credentials: req.Credentials,
		Refresh:     req.Refresh,
		ObtainedAt:  now.Unix(),
	}

	writer := h.connectorWriterFor(req.Connector)
	if writer == nil {
		_ = h.publishDataResult(env.Kind, "failure", fmt.Sprintf("no writer for connector %q", req.Connector), nil)
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), connectorHandlerTimeout)
	defer cancel()
	if err := writer.Write(ctx, creds); err != nil {
		slog.Error("connector.set: write failed", "component", "mqtt", "kind", env.Kind, "connector", req.Connector, "error", err)
		h.alertOps("❌ connector.set "+req.Connector+" — FAILED", err.Error())
		_ = h.publishDataResult(env.Kind, "failure", err.Error(), map[string]interface{}{
			"connector": req.Connector,
		})
		return
	}
	slog.Info("connector.set: applied", "component", "mqtt", "connector", req.Connector, "auth_type", req.AuthType, "refresh", req.Refresh)
	h.alertOps("✅ Connector "+req.Connector+" connected (auth_type="+req.AuthType+")", "")
	_ = h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"connector": req.Connector,
	})
}

// handleConnectorRemove handles kind="connector.remove.<code>".
// Per contract reply: `data.removed` is true when the connector entry existed
// (and was deleted), false when no-op. Restart side-effects (if any) are
// owned by the writer, not the dispatcher.
func (h *DeviceMQTTHandler) handleConnectorRemove(env domain.MQTTDataCommand) error {
	if err := h.publishDataResult(env.Kind, "starting", "", nil); err != nil {
		slog.Error("connector.remove: ack publish failed", "component", "mqtt", "kind", env.Kind, "error", err)
	}

	go h.runConnectorRemove(env)
	return nil
}

func (h *DeviceMQTTHandler) runConnectorRemove(env domain.MQTTDataCommand) {
	var req domain.MQTTConnectorRemoveData
	if len(env.Data) == 0 {
		_ = h.publishDataResult(env.Kind, "failure", "invalid "+env.Kind+" data: empty", nil)
		return
	}
	if err := json.Unmarshal(env.Data, &req); err != nil {
		_ = h.publishDataResult(env.Kind, "failure", "invalid "+env.Kind+" data: "+err.Error(), nil)
		return
	}
	if req.Connector == "" {
		_ = h.publishDataResult(env.Kind, "failure", "connector is required", nil)
		return
	}

	writer := h.connectorWriterFor(req.Connector)
	if writer == nil {
		_ = h.publishDataResult(env.Kind, "failure", fmt.Sprintf("no writer for connector %q", req.Connector), nil)
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), connectorHandlerTimeout)
	defer cancel()
	removed, err := writer.Remove(ctx, req.Connector)
	if err != nil {
		slog.Error("connector.remove: remove failed", "component", "mqtt", "kind", env.Kind, "connector", req.Connector, "error", err)
		h.alertOps("❌ connector.remove "+req.Connector+" — FAILED", err.Error())
		_ = h.publishDataResult(env.Kind, "failure", err.Error(), map[string]interface{}{
			"connector": req.Connector,
		})
		return
	}
	slog.Info("connector.remove: done", "component", "mqtt", "connector", req.Connector, "removed", removed)
	h.alertOps("✅ connector.remove "+req.Connector+" — OK", "")
	_ = h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"connector": req.Connector,
		"removed":   removed,
	})
}
