package config

import (
	"os"
	"path/filepath"
	"testing"
)

// The skill path depends on config actually consulting syspath: a metadata_url
// that never reaches OTAMetadataURL leaves skillsBaseURL empty and
// downloadSkills silently does nothing. Asserting the syspath default alone
// would not catch a revert to a hardcoded const here, so read through the env.
func TestOTAMetadataURLHonoursBootstrapEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bootstrap.json")
	want := "https://cdn.example/os/ota/metadata.json"
	if err := os.WriteFile(path, []byte(`{"metadata_url":"`+want+`"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	t.Setenv("OS_BOOTSTRAP_CONFIG", path)
	if got := otaMetadataURLFromBootstrap(); got != want {
		t.Errorf("got %q, want %q", got, want)
	}

	// Unset must fall back to the device path, which does not exist off-device
	// — an empty string, never a panic or a stale value.
	t.Setenv("OS_BOOTSTRAP_CONFIG", "")
	if got := otaMetadataURLFromBootstrap(); got != "" {
		t.Errorf("device default resolved to %q on a machine with no /root/config", got)
	}
}
