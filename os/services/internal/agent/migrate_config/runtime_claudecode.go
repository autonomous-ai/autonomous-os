package migrateconfig

import (
	"path/filepath"
)

type claudecodeAdapter struct{}

func (claudecodeAdapter) runtime() Runtime { return RuntimeClaudeCode }

// read extracts LLMConfig from /root/.claudecode/.env (ANTHROPIC_API_KEY +
// ANTHROPIC_BASE_URL) — the file presync.sh writes; reading it directly captures
// any drift introduced after the last presync run.
func (claudecodeAdapter) read(opts Options) (LLMConfig, error) {
	env := filepath.Join(opts.ClaudecodeDir, ".env")
	return LLMConfig{
		APIKey:  readEnvVar(env, "ANTHROPIC_API_KEY"),
		BaseURL: readEnvVar(env, "ANTHROPIC_BASE_URL"),
	}, nil
}

// write updates /root/.claudecode/.env with the canonical config. The same
// fields presync.sh owns, so the result matches a fresh presync run.
// ANTHROPIC_AUTH_TOKEN mirrors ANTHROPIC_API_KEY (presync sets both — claude
// sends x-api-key from the former, Authorization: Bearer from the latter).
func (claudecodeAdapter) write(cfg LLMConfig, opts Options) error {
	env := filepath.Join(opts.ClaudecodeDir, ".env")
	if cfg.BaseURL != "" {
		if err := writeEnvVar(env, "ANTHROPIC_BASE_URL", cfg.BaseURL); err != nil {
			return err
		}
	}
	if cfg.APIKey != "" {
		if err := writeEnvVar(env, "ANTHROPIC_API_KEY", cfg.APIKey); err != nil {
			return err
		}
		if err := writeEnvVar(env, "ANTHROPIC_AUTH_TOKEN", cfg.APIKey); err != nil {
			return err
		}
	}
	return nil
}
