package mqtthandler

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"go-lamp.autonomous.ai/domain"
	"go-lamp.autonomous.ai/internal/device"
)

const (
	accessTokensFile      = "access_tokens.json"
	accessTokensSchemaVer = 1
	accessTokensFileMode  = 0600
	accessTokensDirMode   = 0700
)

// accessTokensMu serializes reads/writes of access_tokens.json across MQTT callbacks.
var accessTokensMu sync.Mutex

func (h *DeviceMQTTHandler) handleOAuthSet(env domain.MQTTDataCommand) error {
	var req domain.MQTTOAuthSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid oauth.set data: "+err.Error(), nil)
	}
	if req.Provider == "" || req.AccessToken == "" {
		return h.publishDataResult(env.Kind, "failure", "provider and access_token are required", nil)
	}

	entry := domain.OAuthTokenEntry{
		AccessToken:  req.AccessToken,
		RefreshToken: req.RefreshToken,
		TokenType:    req.TokenType,
		ExpiresAt:    req.ExpiresAt,
		Scopes:       req.Scopes,
		UserEmail:    req.UserEmail,
		ClientID:     req.ClientID,
		ObtainedAt:   time.Now().Unix(),
	}

	if err := h.upsertOAuthEntry(req.Provider, entry); err != nil {
		slog.Error("oauth.set: store failed", "component", "mqtt", "provider", req.Provider, "error", err)
		return h.publishDataResult(env.Kind, "failure", "store: "+err.Error(), nil)
	}

	slog.Info("oauth.set: stored", "component", "mqtt", "provider", req.Provider, "scopes", req.Scopes)
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"provider": req.Provider,
		"scopes":   req.Scopes,
	})
}

func (h *DeviceMQTTHandler) handleOAuthRemove(env domain.MQTTDataCommand) error {
	var req domain.MQTTOAuthRemoveData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid oauth.remove data: "+err.Error(), nil)
	}
	if req.Provider == "" {
		return h.publishDataResult(env.Kind, "failure", "provider is required", nil)
	}

	removed, err := h.deleteOAuthEntry(req.Provider)
	if err != nil {
		slog.Error("oauth.remove: delete failed", "component", "mqtt", "provider", req.Provider, "error", err)
		return h.publishDataResult(env.Kind, "failure", "delete: "+err.Error(), nil)
	}

	slog.Info("oauth.remove: done", "component", "mqtt", "provider", req.Provider, "removed", removed)
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"provider": req.Provider,
		"removed":  removed,
	})
}

func (h *DeviceMQTTHandler) accessTokensPath() string {
	return filepath.Join(h.config.OpenclawConfigDir, "workspace", "configs", accessTokensFile)
}

// loadAccessTokens reads access_tokens.json. Missing or empty file → fresh struct.
func (h *DeviceMQTTHandler) loadAccessTokens() (domain.AccessTokensFile, error) {
	path := h.accessTokensPath()
	out := domain.AccessTokensFile{Version: accessTokensSchemaVer, Providers: map[string]domain.OAuthTokenEntry{}}

	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return out, nil
		}
		return out, fmt.Errorf("read %s: %w", path, err)
	}
	if len(data) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return out, fmt.Errorf("parse %s: %w", path, err)
	}
	if out.Providers == nil {
		out.Providers = map[string]domain.OAuthTokenEntry{}
	}
	if out.Version == 0 {
		out.Version = accessTokensSchemaVer
	}
	return out, nil
}

// writeAccessTokens persists tokens via tmp+rename so a mid-write crash cannot
// leave a truncated file behind.
func (h *DeviceMQTTHandler) writeAccessTokens(f domain.AccessTokensFile) error {
	path := h.accessTokensPath()
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, accessTokensDirMode); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}

	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	tmp, err := os.CreateTemp(dir, ".access_tokens.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Chmod(accessTokensFileMode); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("chmod tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

func (h *DeviceMQTTHandler) upsertOAuthEntry(provider string, entry domain.OAuthTokenEntry) error {
	accessTokensMu.Lock()
	defer accessTokensMu.Unlock()

	tokens, err := h.loadAccessTokens()
	if err != nil {
		return err
	}
	tokens.Providers[provider] = entry
	return h.writeAccessTokens(tokens)
}

// deleteOAuthEntry returns true if the provider key existed and was removed.
func (h *DeviceMQTTHandler) deleteOAuthEntry(provider string) (bool, error) {
	accessTokensMu.Lock()
	defer accessTokensMu.Unlock()

	tokens, err := h.loadAccessTokens()
	if err != nil {
		return false, err
	}
	if _, ok := tokens.Providers[provider]; !ok {
		return false, nil
	}
	delete(tokens.Providers, provider)
	return true, h.writeAccessTokens(tokens)
}

func (h *DeviceMQTTHandler) publishDataResult(kind, status, errMsg string, data interface{}) error {
	resp := domain.MQTTDataResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             kind,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	return h.publish(resp)
}
