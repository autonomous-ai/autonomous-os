package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

const configPath = "config/bootstrap.json"

// BootstrapVersion is injected at build time via ldflags.
// Example:
//
//	-X go-lamp.autonomous.ai/bootstrap/config.BootstrapVersion=v1.2.3
var BootstrapVersion = "dev"

// Config holds bootstrap OTA worker configuration.
// All fields are stored in config/bootstrap.json (no CLI args).
type Config struct {
	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	MetadataURL  string `json:"metadata_url" yaml:"metadataURL"`
	PollInterval string `json:"poll_interval" yaml:"pollInterval"` // e.g. "1h", "10m"
	StateFile    string `json:"state_file" yaml:"stateFile"`
}

// Default returns default bootstrap config.
func Default() Config {
	return Config{
		HttpPort:     8080,
		MetadataURL:  "https://cdn.autonomous.ai/lamp/ota/metadata.json",
		PollInterval: "5m",
		StateFile:    "/root/bootstrap/state.json",
	}
}

// Load reads config from configPath. Returns error if file is missing or invalid.
func Load() (*Config, error) {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		return nil, fmt.Errorf("config file not found: %s", configPath)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", configPath, err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", configPath, err)
	}
	return &cfg, nil
}

// LoadOrDefault loads config from file, or returns Default() if file is missing.
func LoadOrDefault() *Config {
	cfg, err := Load()
	if err != nil {
		d := Default()
		return &d
	}
	return cfg
}

// Save writes the config to the config file.
func (c *Config) Save() error {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}
	dir := filepath.Dir(configPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	if err := os.WriteFile(configPath, data, 0600); err != nil {
		return fmt.Errorf("write config %s: %w", configPath, err)
	}
	return nil
}
