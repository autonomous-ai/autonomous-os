package migrateconfig

import (
	"os"
	"path/filepath"

	"github.com/pelletier/go-toml/v2"
)

type codexAdapter struct{}

func (codexAdapter) runtime() Runtime { return RuntimeCodex }

// Codex splits the LLM config across two presync-owned files under
// /root/.codex: config.toml carries [model_providers.autonomous].base_url,
// and .env carries the actual key (OPENAI_API_KEY=… — config.toml only holds
// the env_key NAME). Mirrors the hermes adapter, which reads its own
// presync-owned config.yaml/.env the same way.
func (codexAdapter) read(opts Options) (LLMConfig, error) {
	return LLMConfig{
		APIKey:  readEnvVar(filepath.Join(opts.CodexHome, ".env"), "OPENAI_API_KEY"),
		BaseURL: readCodexBaseURL(filepath.Join(opts.CodexHome, "config.toml")),
	}, nil
}

func (codexAdapter) write(cfg LLMConfig, opts Options) error {
	if cfg.APIKey != "" {
		if err := writeEnvVar(filepath.Join(opts.CodexHome, ".env"), "OPENAI_API_KEY", cfg.APIKey); err != nil {
			return err
		}
	}
	if cfg.BaseURL != "" {
		if err := writeCodexBaseURL(filepath.Join(opts.CodexHome, "config.toml"), cfg.BaseURL); err != nil {
			return err
		}
	}
	return nil
}

// readCodexBaseURL extracts model_providers.autonomous.base_url from
// config.toml. Missing file/keys → "" (nothing to carry).
func readCodexBaseURL(configTOML string) string {
	raw, err := os.ReadFile(configTOML)
	if err != nil {
		return ""
	}
	root := map[string]any{}
	if err := toml.Unmarshal(raw, &root); err != nil {
		return ""
	}
	providers, _ := root["model_providers"].(map[string]any)
	autonomous, _ := providers["autonomous"].(map[string]any)
	baseURL, _ := autonomous["base_url"].(string)
	return baseURL
}

// writeCodexBaseURL patches model_providers.autonomous.base_url in config.toml
// via a TOML round-trip (the [mcp_servers] entries survive the map round-trip).
// A missing config.toml is not an error — presync regenerates the whole head
// from config.json (already synced by the caller) on the next switch/boot.
func writeCodexBaseURL(configTOML, baseURL string) error {
	raw, err := os.ReadFile(configTOML)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	root := map[string]any{}
	if err := toml.Unmarshal(raw, &root); err != nil {
		return err
	}

	providers := ensureMap(root, "model_providers")
	autonomous := ensureMap(providers, "autonomous")
	if autonomous["base_url"] == baseURL {
		return nil
	}
	autonomous["base_url"] = baseURL

	out, err := toml.Marshal(root)
	if err != nil {
		return err
	}
	return atomicWrite(configTOML, out, 0o644)
}
