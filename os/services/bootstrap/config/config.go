package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// configPath is the bootstrap worker's own config file. It lives next to
// lamp-server's config.json under /root/config so all device config sits in one
// place — but the bootstrap worker keeps a file separate from config.json.
const configPath = "/root/config/bootstrap.json"

// BootstrapVersion is injected at build time via ldflags.
// Example:
//
//	-X go.autonomous.ai/os/bootstrap/config.BootstrapVersion=v1.2.3
var BootstrapVersion = "dev"

// Config holds bootstrap OTA worker configuration.
// All fields are stored in /root/config/bootstrap.json (no CLI args).
type Config struct {
	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	MetadataURL  string `json:"metadata_url" yaml:"metadataURL"`
	PollInterval string `json:"poll_interval" yaml:"pollInterval"` // e.g. "1h", "10m"
	StateFile    string `json:"state_file" yaml:"stateFile"`
}

// Default returns the bootstrap config with operational defaults. MetadataURL is
// intentionally empty — it is a per-deployment value seeded into bootstrap.json
// at provisioning, never compiled into the binary.
func Default() Config {
	return Config{
		HttpPort:     8080,
		MetadataURL:  "",
		PollInterval: "5m",
		StateFile:    "/root/bootstrap/state.json",
	}
}

// LoadOrDefault overlays bootstrap.json onto Default(): fields present in the
// file win, absent fields keep their operational default. A missing or corrupt
// file yields pure defaults — MetadataURL stays empty so the caller waits for
// provisioning to populate it.
func LoadOrDefault() *Config {
	cfg := Default()
	data, err := os.ReadFile(configPath)
	if err != nil {
		return &cfg
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		d := Default()
		return &d
	}
	return &cfg
}

// Save writes the config to /root/config/bootstrap.json.
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
