package migrateconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type openclawAdapter struct{}

func (openclawAdapter) runtime() Runtime { return RuntimeOpenclaw }

const openclawProviderName = "autonomous"

// read extracts LLMConfig from ~/.openclaw/openclaw.json under
// models.providers.autonomous.{apiKey,baseUrl}.
func (openclawAdapter) read(opts Options) (LLMConfig, error) {
	data, err := os.ReadFile(filepath.Join(opts.OpenclawConfigDir, "openclaw.json"))
	if err != nil {
		return LLMConfig{}, nil // file absent → nothing to read
	}
	var root map[string]interface{}
	if err := json.Unmarshal(data, &root); err != nil {
		return LLMConfig{}, nil
	}
	models, _ := root["models"].(map[string]interface{})
	providers, _ := models["providers"].(map[string]interface{})
	autonomous, _ := providers[openclawProviderName].(map[string]interface{})
	apiKey, _ := autonomous["apiKey"].(string)
	baseURL, _ := autonomous["baseUrl"].(string)
	return LLMConfig{APIKey: apiKey, BaseURL: baseURL}, nil
}

// write updates models.providers.autonomous.{apiKey,baseUrl} in openclaw.json.
// Only the two auth fields are touched; the models list and other provider fields
// are left in place so ensureProviderConfig (the fallback) can fill them in if
// the catalog is stale.
func (openclawAdapter) write(cfg LLMConfig, opts Options) error {
	configPath := filepath.Join(opts.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return err
	}
	var root map[string]interface{}
	if err := json.Unmarshal(data, &root); err != nil {
		return err
	}

	models := ensureMap(root, "models")
	providers := ensureMap(models, "providers")
	autonomous := ensureMap(providers, openclawProviderName)

	changed := false
	if cfg.APIKey != "" && autonomous["apiKey"] != cfg.APIKey {
		autonomous["apiKey"] = cfg.APIKey
		changed = true
	}
	if cfg.BaseURL != "" && autonomous["baseUrl"] != cfg.BaseURL {
		autonomous["baseUrl"] = cfg.BaseURL
		changed = true
	}
	if !changed {
		return nil
	}

	providers[openclawProviderName] = autonomous
	models["providers"] = providers
	root["models"] = models

	out, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(configPath, out, 0o600)
}

func ensureMap(parent map[string]interface{}, key string) map[string]interface{} {
	if m, ok := parent[key].(map[string]interface{}); ok {
		return m
	}
	m := map[string]interface{}{}
	parent[key] = m
	return m
}
