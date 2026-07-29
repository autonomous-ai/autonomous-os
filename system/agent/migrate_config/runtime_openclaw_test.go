package migrateconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// workspaceValue reads agents.defaults.workspace back out of the written openclaw.json.
func workspaceValue(t *testing.T, configPath string) string {
	t.Helper()
	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var root map[string]interface{}
	if err := json.Unmarshal(data, &root); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	agents, _ := root["agents"].(map[string]interface{})
	defaults, _ := agents["defaults"].(map[string]interface{})
	ws, _ := defaults["workspace"].(string)
	return ws
}

func writeConfig(t *testing.T, dir string, root map[string]interface{}) string {
	t.Helper()
	configPath := filepath.Join(dir, "openclaw.json")
	b, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, b, 0o600); err != nil {
		t.Fatal(err)
	}
	return configPath
}

// TestOpenclawWrite_PinsWorkspaceFromNested is the core guard: a config carrying the
// nested double-".openclaw" workspace must be corrected to the hardcoded default on a
// switch TO openclaw, even when the LLM auth fields are unchanged.
func TestOpenclawWrite_PinsWorkspaceFromNested(t *testing.T) {
	dir := t.TempDir()
	configPath := writeConfig(t, dir, map[string]interface{}{
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"workspace": "/root/.openclaw/.openclaw/workspace", // the drift
			},
		},
	})

	// No apiKey/baseUrl change — the workspace pin alone must make it write.
	if err := (openclawAdapter{}).write(LLMConfig{}, Options{OpenclawConfigDir: dir}); err != nil {
		t.Fatalf("write: %v", err)
	}

	if got := workspaceValue(t, configPath); got != openclawDefaultWorkspace {
		t.Fatalf("workspace not pinned: got %q, want %q", got, openclawDefaultWorkspace)
	}
}

// TestOpenclawWrite_PinsWorkspaceAlongsideAuth verifies the pin also happens when the
// migration is carrying auth across, and that the auth fields land too.
func TestOpenclawWrite_PinsWorkspaceAlongsideAuth(t *testing.T) {
	dir := t.TempDir()
	configPath := writeConfig(t, dir, map[string]interface{}{
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				openclawProviderName: map[string]interface{}{"apiKey": "old"},
			},
		},
	})

	err := (openclawAdapter{}).write(LLMConfig{APIKey: "new-key", BaseURL: "https://api.example"}, Options{OpenclawConfigDir: dir})
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	if got := workspaceValue(t, configPath); got != openclawDefaultWorkspace {
		t.Fatalf("workspace not pinned: got %q, want %q", got, openclawDefaultWorkspace)
	}
	data, _ := os.ReadFile(configPath)
	var root map[string]interface{}
	_ = json.Unmarshal(data, &root)
	models, _ := root["models"].(map[string]interface{})
	providers, _ := models["providers"].(map[string]interface{})
	autonomous, _ := providers[openclawProviderName].(map[string]interface{})
	if autonomous["apiKey"] != "new-key" || autonomous["baseUrl"] != "https://api.example" {
		t.Fatalf("auth not written: %+v", autonomous)
	}
}

// TestOpenclawWrite_NoopWhenAlreadyCanonical ensures we don't rewrite when the
// workspace is already the default and there is no auth change.
func TestOpenclawWrite_NoopWhenAlreadyCanonical(t *testing.T) {
	dir := t.TempDir()
	configPath := writeConfig(t, dir, map[string]interface{}{
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{"workspace": openclawDefaultWorkspace},
		},
	})
	before, _ := os.Stat(configPath)

	if err := (openclawAdapter{}).write(LLMConfig{}, Options{OpenclawConfigDir: dir}); err != nil {
		t.Fatalf("write: %v", err)
	}

	after, _ := os.Stat(configPath)
	if !before.ModTime().Equal(after.ModTime()) {
		t.Fatalf("expected no rewrite when already canonical + no auth change")
	}
	if got := workspaceValue(t, configPath); got != openclawDefaultWorkspace {
		t.Fatalf("workspace changed unexpectedly: %q", got)
	}
}
