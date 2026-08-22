package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// configPath is the bootstrap worker's own config file. It lives next to
// os-server's config.json under /root/config so all device config sits in one
// place — but the bootstrap worker keeps a file separate from config.json.
const configPath = "/root/config/bootstrap.json"

// BootstrapVersion is injected at build time via ldflags.
// Example:
//
//	-X go.autonomous.ai/os/system/bootstrap/config.BootstrapVersion=v1.2.3
var BootstrapVersion = "dev"

// Config holds bootstrap OTA worker configuration.
// All fields are stored in /root/config/bootstrap.json (no CLI args).
type Config struct {
	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	MetadataURL string `json:"metadata_url" yaml:"metadataURL"`
	// SigningPublicKey is the base64-encoded, 32-byte Ed25519 public key that
	// authorizes OTA metadata for this deployment. It is provisioned locally and
	// is never accepted from the metadata feed itself.
	SigningPublicKey string `json:"signing_public_key" yaml:"signingPublicKey"`
	// OtaFrozen is a local operator freeze. The owner-facing freeze also lives
	// on the custody record (ota_frozen); either one refuses apply.
	OtaFrozen bool `json:"ota_frozen,omitempty" yaml:"otaFrozen"`
	// CustodyFile is the ABP custody record. After handoff leftover_closed
	// requires a signature; ota_frozen refuses apply. Missing file is legacy.
	CustodyFile string `json:"custody_file,omitempty" yaml:"custodyFile"`
	// RollbackVersions records release versions explicitly rolled back by the
	// local updater. Bootstrap skips only an exact matching target, allowing a
	// later metadata version to resume automatic OTA without operator cleanup.
	RollbackVersions map[string]string `json:"rollback_versions,omitempty" yaml:"rollbackVersions"`
	PollInterval     string            `json:"poll_interval" yaml:"pollInterval"` // e.g. "1h", "10m"
	StateFile        string            `json:"state_file" yaml:"stateFile"`
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
		CustodyFile:  "/var/lib/lamp/custody.json",
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
