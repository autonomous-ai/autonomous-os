package migrateconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type picoclawAdapter struct{}

func (picoclawAdapter) runtime() Runtime { return RuntimePicoclaw }

// PicoClaw uses the same openclaw.json layout under ~/.picoclaw/.
func (picoclawAdapter) read(opts Options) (LLMConfig, error) {
	data, err := os.ReadFile(filepath.Join(opts.PicoclawConfigDir, "openclaw.json"))
	if err != nil {
		return LLMConfig{}, nil
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

func (picoclawAdapter) write(cfg LLMConfig, opts Options) error {
	configPath := filepath.Join(opts.PicoclawConfigDir, "openclaw.json")
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
