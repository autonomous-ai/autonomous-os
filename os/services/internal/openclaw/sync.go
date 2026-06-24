package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/domain"
	"go.autonomous.ai/os/server/config"
)

// SyncModelsFromAPI fetches the live model list from ModelsAPIURL and
// reconciles it into openclaw.json under s.config.OpenclawConfigDir. The model
// catalog is OVERWRITTEN (not merged):
//   - providers.autonomous.models[] is replaced 1:1 with the fetched list —
//     stale entries removed, new entries added.
//   - agents.defaults.models autonomous/* keys are reconciled to exactly match
//     the fetched keys (legacy unprefixed entries purged). Keys for other
//     providers (e.g. "venice/...") are left untouched.
//
// When the fetched catalog version is newer than config.DefaultModelVersion it
// also applies the upstream default_model and default_image_model (each gated
// to skip fields the user manually pointed at another provider), then persists
// the new version (and primary) into config.
//
// No-op (returns false, nil) when openclaw.json is missing or the provider
// section is absent. A failed fetch / invalid JSON returns an error so the
// caller can decide to fall back. The caller must NOT treat any of these as
// fatal — the device must keep running.
//
// Restarts the openclaw gateway only when the file actually changed.
// Holds primarySyncMu for the entire read-modify-write cycle so it cannot
// interleave with other openclaw.json writers (watcher, refresh, setup).
// The network fetch happens before the lock to keep the critical section short.
func (s *OpenclawService) SyncModelsFromAPI() (bool, error) {
	resp, err := FetchModelsFromAPI()
	if err != nil {
		return false, fmt.Errorf("fetch models: %w", err)
	}

	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]interface{}
	if err := json.Unmarshal(raw, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw config: %w", err)
	}

	autonomousMap, ok := autonomousProviderMap(configData)
	if !ok {
		return false, nil
	}

	return s.applyModelsToConfig(configPath, configData, autonomousMap, resp)
}

// StartModelSync runs the periodic model sync loop until ctx is cancelled.
// Eager first tick on entry, then a steady ticker at ModelSyncInterval. Each
// tick is wrapped in panic recovery so a third-party JSON parser regression
// can't kill the loop. A failed sync logs and continues — the device must
// keep running.
func (s *OpenclawService) StartModelSync(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("[modelsync] PANIC recovered, sync loop stopped", "panic", r)
		}
	}()

	tick := func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("[modelsync] tick PANIC recovered", "panic", r)
			}
		}()
		if _, err := s.SyncModelsFromAPI(); err != nil {
			slog.Warn("[modelsync] tick failed", "err", err)
		}
	}

	tick()
	ticker := time.NewTicker(ModelSyncInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			tick()
		}
	}
}

// FetchModelsFromAPI does the actual HTTP GET against ModelsAPIURL (tunables.go)
// and returns the upstream model list. Used by the periodic SyncModelsFromAPI
// loop. Returns a typed error on transport, status, or JSON-shape failures so
// callers can skip the tick without crashing the device.
func FetchModelsFromAPI() (*domain.LLMModelsListResponse, error) {
	url := strings.TrimSpace(ModelsAPIURL)
	if url == "" {
		return nil, fmt.Errorf("empty models api url (check tunables.go)")
	}

	ctx, cancel := context.WithTimeout(context.Background(), modelsAPITimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build models request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("fetch %s: status %d", url, resp.StatusCode)
	}

	var out domain.LLMModelsListResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode models response: %w", err)
	}
	if len(out.Models) == 0 {
		return nil, fmt.Errorf("models response is empty")
	}
	return &out, nil
}

// autonomousProviderMap drills into models.providers.autonomous, returning the
// inner map and ok=true only when every level exists.
func autonomousProviderMap(configData map[string]any) (map[string]any, bool) {
	modelsMap, _ := configData["models"].(map[string]any)
	if modelsMap == nil {
		return nil, false
	}
	providersMap, _ := modelsMap["providers"].(map[string]any)
	if providersMap == nil {
		return nil, false
	}
	autonomousMap, _ := providersMap[customProviderName].(map[string]any)
	if autonomousMap == nil {
		return nil, false
	}
	return autonomousMap, true
}

// applyModelsToConfig overwrites the autonomous model catalog in both
// providers.autonomous.models and agents.defaults.models, applies the
// version-gated default text/image model, writes the file when anything
// changed, restarts the openclaw gateway, and persists the applied catalog
// version (and resolved primary) into config. Idempotent.
func (s *OpenclawService) applyModelsToConfig(configPath string, configData map[string]any, autonomousMap map[string]any, resp *domain.LLMModelsListResponse) (bool, error) {
	fetched := resp.Models

	existingProvider, _ := autonomousMap["models"].([]any)
	newProvider, providerChanged := overwriteProviderModels(existingProvider, fetched)

	// Overwrite the provider wire protocol from upstream (fallback to the
	// built-in default when omitted). Always reconciled, like the catalog —
	// not version-gated.
	apiType := resolveAutonomousAPI(resp.API)
	var apiChanged bool
	if cur, _ := autonomousMap["api"].(string); cur != apiType {
		apiChanged = true
	}

	var agentChanged bool
	if agentsMap, ok := configData["agents"].(map[string]any); ok {
		if defaultsMap, ok := agentsMap["defaults"].(map[string]any); ok {
			existingAgent, _ := defaultsMap["models"].(map[string]any)
			merged, changed := overwriteAgentAutonomousModels(existingAgent, fetched)
			if changed {
				defaultsMap["models"] = merged
				agentChanged = true
			}
		}
	}

	// Version-gated default model / image model. Only when upstream published a
	// newer catalog version, and only for fields still on the autonomous
	// provider (preserve a user's manual provider switch).
	applyDefaults := resp.Version > 0 && resp.Version > s.config.DefaultModelVersion
	var primaryChanged, imageChanged bool
	if applyDefaults {
		if isDefaultsOnAutonomous(configData) {
			primaryChanged = applyDefaultPrimaryModel(configData, strings.TrimSpace(resp.DefaultModel))
		}
		if isImageModelOnAutonomous(configData) {
			imageChanged = applyDefaultImageModel(configData, strings.TrimSpace(resp.DefaultImageModel))
		}
	}

	if providerChanged {
		autonomousMap["models"] = newProvider
	}
	if apiChanged {
		autonomousMap["api"] = apiType
	}

	if !providerChanged && !apiChanged && !agentChanged && !primaryChanged && !imageChanged {
		// File already in desired state. Still record that we've seen this
		// catalog version so the gate stops re-evaluating it every tick.
		if applyDefaults {
			s.persistModelState(resp.Version, "")
		}
		return false, nil
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return false, fmt.Errorf("marshal openclaw config: %w", err)
	}
	// The flag value must equal whatever primary the file now carries so the
	// watcher recognises this as a Lamp write and does not sync it back. When
	// primaryChanged is true, extractPrimaryModel already returns the new value.
	setOSWriteFlag(filepath.Dir(configPath), extractPrimaryModel(configData))
	if err := atomicWriteFile(configPath, written, 0600); err != nil {
		return false, fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return false, fmt.Errorf("set openclaw config ownership: %w", err)
	}

	// Persist version + LLMModel. Only sync LLMModel when we actually rewrote the
	// primary to an autonomous default (mirrors the watcher's autonomous-only
	// sync-back rule).
	if applyDefaults {
		newModel := ""
		if primaryChanged {
			_, newModel, _ = splitProviderModel(extractPrimaryModel(configData))
		}
		s.persistModelState(resp.Version, newModel)
	}

	slog.Info("[modelsync] reconciled openclaw config",
		"path", configPath,
		"provider_changed", providerChanged,
		"api_changed", apiChanged,
		"agent_changed", agentChanged,
		"primary_changed", primaryChanged,
		"image_changed", imageChanged,
		"version", resp.Version,
		"fetched", len(fetched),
	)

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[modelsync] restart openclaw gateway", "err", err)
	}
	return true, nil
}

// persistModelState records the applied catalog version (and optionally the new
// primary model) into config under the config mutex. newModel == "" leaves
// LLMModel untouched. version only advances (never regresses).
func (s *OpenclawService) persistModelState(version int, newModel string) {
	if err := s.config.WithLockSave(func(c *config.Config) {
		if version > c.DefaultModelVersion {
			c.DefaultModelVersion = version
		}
		if newModel != "" {
			c.LLMModel = newModel
		}
	}); err != nil {
		slog.Warn("[modelsync] persist model state", "err", err)
	}
}

// resolveAutonomousAPI returns the wire protocol to write into
// models.providers.autonomous.api: the upstream-published value when present,
// otherwise the built-in fallback (autonomousProviderAPI).
func resolveAutonomousAPI(api string) string {
	if v := strings.TrimSpace(api); v != "" {
		return v
	}
	return autonomousProviderAPI
}

// overwriteProviderModels REPLACES the providers.autonomous.models[] slice with
// the fetched list. Existing entries whose id is in fetched have their full
// payload refreshed from openclawModelToProviderEntry (local edits discarded —
// overwrite, not merge); entries whose id is NOT in fetched are dropped.
//
// Returns (newSlice, changed) where changed=false when the result is equivalent
// to existing (same length, same ids in order, same numeric fields). The
// returned slice follows the fetched order so the result is deterministic.
func overwriteProviderModels(existing []any, fetched []domain.LLMModel) ([]any, bool) {
	out := make([]any, 0, len(fetched))
	for _, m := range fetched {
		out = append(out, openclawModelToProviderEntry(m))
	}
	if len(existing) != len(out) {
		return out, true
	}
	for i, freshAny := range out {
		fresh := freshAny.(map[string]any)
		oldEntry, ok := existing[i].(map[string]any)
		if !ok {
			return out, true
		}
		if oldID, _ := oldEntry["id"].(string); oldID != fresh["id"].(string) {
			return out, true
		}
		if !numbersEqual(oldEntry["contextWindow"], fresh["contextWindow"]) {
			return out, true
		}
		if !numbersEqual(oldEntry["maxTokens"], fresh["maxTokens"]) {
			return out, true
		}
	}
	return out, false
}

// numbersEqual compares two JSON-decoded numeric values that may be int,
// int64, or float64 depending on whether they came from a Go literal or from
// json.Unmarshal into map[string]any. Returns false if either side is not a
// number.
func numbersEqual(a, b any) bool {
	av, aOk := toFloat(a)
	bv, bOk := toFloat(b)
	if !aOk || !bOk {
		return false
	}
	return av == bv
}

func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case int:
		return float64(x), true
	case int32:
		return float64(x), true
	case int64:
		return float64(x), true
	case float32:
		return float64(x), true
	case float64:
		return x, true
	}
	return 0, false
}

// overwriteAgentAutonomousModels reconciles agents.defaults.models so the set
// of "autonomous-owned" keys exactly matches the fetched list:
//
//  1. Keys with the "autonomous/" prefix not in fetched are REMOVED.
//  2. Keys with NO "/" separator are REMOVED — pre-prefix-era legacy
//     autonomous catalog entries (e.g. "claude-haiku-4-5").
//
// Keys with a slash but a different provider prefix (e.g. "venice/x") are LEFT
// UNTOUCHED — the firmware can't tell whether they belong to a provider it
// doesn't manage, so it never deletes them. Missing "autonomous/<key>" entries
// are added with empty value {}.
//
// Returns (newMap, changed) where changed=false iff the result equals existing.
func overwriteAgentAutonomousModels(existing map[string]any, fetched []domain.LLMModel) (map[string]any, bool) {
	out := make(map[string]any, len(existing)+len(fetched))
	for k, v := range existing {
		out[k] = v
	}
	wanted := make(map[string]struct{}, len(fetched))
	for _, m := range fetched {
		if m.Key == "" {
			continue
		}
		wanted[agentModelKey(m)] = struct{}{}
	}
	changed := false
	prefix := customProviderName + "/"
	for k := range existing {
		switch {
		case strings.HasPrefix(k, prefix):
			// Rule 1: autonomous/* must match the wanted set.
			if _, keep := wanted[k]; !keep {
				delete(out, k)
				changed = true
			}
		case !strings.Contains(k, "/"):
			// Rule 2: unprefixed → pre-prefix-era legacy autonomous, purge.
			delete(out, k)
			changed = true
		}
		// Other slashed keys (e.g. "venice/x") are left untouched.
	}
	for want := range wanted {
		if _, ok := out[want]; !ok {
			out[want] = map[string]any{}
			changed = true
		}
	}
	return out, changed
}

// agentModelKey returns the key used under agents.defaults.models for a given
// provider model. The "{provider}/{key}" shape keeps the openclaw gateway's
// /models listing grouped under a single provider (otherwise it splits ids
// like "minimax/minimax-m2.7" on the first slash and shows them as separate
// providers).
func agentModelKey(m domain.LLMModel) string {
	return customProviderName + "/" + m.Key
}

// atomicWriteFile writes data to path so that concurrent readers — and most
// importantly an unexpected power loss between the open-truncate and the
// final write — never observe a half-written file. Implemented via the
// standard write-temp-then-rename pattern: rename(2) on POSIX is guaranteed
// to either expose the new file in full or leave the old file intact. If the
// process is killed mid-write, the temp file is left behind harmlessly and
// the next sync tick will overwrite it.
func atomicWriteFile(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".openclaw-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpPath := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpPath) }

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("fsync temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Chmod(tmpPath, perm); err != nil {
		cleanup()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}
